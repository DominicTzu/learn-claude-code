"""只讲 s19 新增的逻辑：连接 -> 发现工具 -> 组装工具池 -> 路由调用。

运行：python s19_mcp_plugin/mcp_teaching.py

不需要 API key，也不调用模型。下面用预先写好的 tool_calls 代替模型输出，
让你每次运行都能看到相同的过程，不被模型的随机选择干扰。
注意：这是同一 Python 进程里的模拟，不是一个真正的 MCP 客户端/服务器。
真正跨进程的版本见同目录 mcp_plugin.py。
"""

import json
import re


class MockDocsServer:
    """模拟外部服务；既提供工具说明，也负责具体业务。"""

    def list_tools(self):
        # 工具说明不是函数源码。客户端只需知道名字、用途和参数格式。
        # inputSchema 是 MCP 工具定义使用的字段名，内容遵循 JSON Schema。
        return [
            {
                "name": "search",
                "description": "按关键词搜索本示例的三条教学笔记。",
                "inputSchema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
            }
        ]

    def call_tool(self, name, arguments):
        # 这是服务端的业务实现。真正的服务端也必须有人写这一部分，
        # MCP 只统一通信规则，不会自动替你实现搜索、浏览器或 Notion。
        if name != "search":
            raise ValueError(f"服务端没有这个工具：{name}")
        if set(arguments) != {"query"} or not isinstance(arguments["query"], str):
            raise ValueError("search 需要且只接受一个字符串参数 query")

        notes = [
            "MCP 用统一的消息格式描述和调用工具。",
            "Playwright MCP 的服务器实现了具体的浏览器操作。",
            "模型提出调用请求，宿主程序检查后才真正执行。",
        ]
        matches = [note for note in notes if arguments["query"].lower() in note.lower()]
        return {"isError": False, "content": [{"type": "text", "text": "\n".join(matches) or "没有找到。"}]}


class MockClient:
    """模拟客户端；真实版本会把方法调用变成跨进程的协议消息。"""

    def __init__(self, server):
        self.server = server
        self.tools = server.list_tools()

    def call_tool(self, name, arguments):
        # 这一行直接访问 Python 对象，没有管道、网络或 JSON-RPC 握手。
        return self.server.call_tool(name, arguments)


CONNECT_TOOL = {
    "type": "function",
    "function": {
        "name": "connect_mcp",
        "description": "连接预先配置好的 docs 服务，并发现它的工具。",
        "parameters": {
            "type": "object",
            "properties": {"server": {"type": "string", "enum": ["docs"]}},
            "required": ["server"],
            "additionalProperties": False,
        },
    },
}


def connect_mcp(server_name, connected):
    # 地址/启动命令必须先由用户或管理员配置好。这里的发现不是扫描互联网。
    # 只允许固定的 docs，不让模型提交任意 shell 命令启动陌生程序。
    if server_name != "docs":
        raise ValueError("本示例只配置了 docs 服务")
    if server_name not in connected:
        connected[server_name] = MockClient(MockDocsServer())
    return "docs 已连接；下次请求模型时会把 search 工具的说明一起发过去。"


def assemble_tool_pool(connected):
    tools = [CONNECT_TOOL]
    routes = {}
    for server_name, client in connected.items():
        for tool in client.tools:
            # 前缀属于本宿主的命名约定，不是 MCP 强制格式。
            # 发给服务端时，仍要还原成原名 search。
            alias = re.sub(r"[^a-zA-Z0-9_-]", "_", f"mcp__{server_name}__{tool['name']}")
            if len(alias) > 64 or alias in routes:
                raise ValueError(f"工具别名过长或发生冲突：{alias}")
            tools.append({
                "type": "function",
                "function": {
                    "name": alias,
                    "description": tool["description"],
                    "parameters": tool["inputSchema"],
                },
            })
            # 明确保存两个东西：找哪个客户端、使用服务端的哪个原名。
            # 不用复杂的 lambda 闭包，也不会把其他服务器的 search 调错。
            routes[alias] = (client, tool["name"])
    return tools, routes


def dispatch(call, connected, routes):
    name = call["function"]["name"]
    arguments = json.loads(call["function"]["arguments"])
    if not isinstance(arguments, dict):
        raise ValueError("工具参数必须是 JSON 对象")
    if name == "connect_mcp":
        return connect_mcp(arguments["server"], connected)
    if name not in routes:
        raise ValueError(f"当前工具池没有 {name}")
    client, original_name = routes[name]
    return client.call_tool(original_name, arguments)


def main():
    connected = {}
    messages = [{"role": "user", "content": "查一下 MCP 的笔记。"}]
    # 这些是人工写好的演示数据，不是实际模型响应。
    simulated_calls = [
        {"id": "call_1", "type": "function", "function": {
            "name": "connect_mcp", "arguments": '{"server":"docs"}'}},
        {"id": "call_2", "type": "function", "function": {
            "name": "mcp__docs__search", "arguments": '{"query":"MCP"}'}},
    ]
    for call in simulated_calls:
        tools, routes = assemble_tool_pool(connected)
        print("\n这次模型能选择：", [tool["function"]["name"] for tool in tools])
        print("假定模型返回：", call["function"])
        messages.append({"role": "assistant", "content": None, "tool_calls": [call]})
        result = dispatch(call, connected, routes)
        # call_1/call_2 把一次工具结果与那次模型请求配对。
        # 真实 MCP 消息还会有自己的请求 id，和这里不是同一种编号。
        messages.append({
            "role": "tool", "tool_call_id": call["id"],
            "content": json.dumps(result, ensure_ascii=False),
        })
        print("本地执行后返回：", json.dumps(result, ensure_ascii=False))
    print("\n下一次应把以上 messages 发给模型，让模型根据结果回答用户。")
    print("教学版到此结束；没有生成假装来自模型的最终回答。")


if __name__ == "__main__":
    main()
