# s19 详解：MCP 到底连接了什么，谁真正执行操作？

这份说明配合两个新文件阅读，原来的 `code.py` 和 README 保持不变：

| 文件 | 用来理解什么 | 是否真正使用 MCP |
| --- | --- | --- |
| `mcp_teaching.py` | 连接、发现工具、组装工具池、按名字分发 | 否，同进程的 Python 模拟 |
| `mcp_plugin.py` | 官方 SDK 连接微软 Playwright MCP，并真实操作浏览器 | 是，独立进程之间的协议通信 |
| `requirements_mcp.txt` | 实用版单独的 Python 依赖 | 教学版不需要这些依赖 |

建议先读第 1～4 节，再运行教学版，最后运行真实版的 `list`、`call`、`chat`。
不需要先理解团队协作、定时任务、worktree 或记忆系统。

## 1. 先直接回答：MCP 是连接服务，还是特定服务接口？

**MCP 本身是协议，不是一台中转服务器，也不自带浏览器、文件管理或 Notion 功能。**

协议的意思是：两段程序约定好“怎样介绍自己、怎样公布能力、请求怎么写、结果怎么返回”。
它不会凭空产生搜索、点击或写文件的能力。

**一个具体的 MCP server，则是实现了这套协议的程序，通常同时提供具体业务能力。**

例如：

- Playwright MCP server：接收 `browser_navigate` 等请求，内部使用 Playwright 操作浏览器。
- Notion MCP server：提供查找、读取、更新 Notion 内容等工具，并处理对应的业务请求。
- 你写的公司部署 MCP server：可以提供 `get_deployment_status`、`deploy_service`，内部再调用公司系统。

因此，它不是“一个不涉及业务、只连接两个端口的通用服务”。更准确地说：

> MCP 统一外面的通信规则；具体服务器实现里面的业务操作。

一个服务器可以提供多个服务的工具，也可以只提供一种业务；没有“一台服务器只能对应一个产品”的规定。
有些部署会再加网关，负责鉴权、转发、审计，但**网关是可选架构，不是 MCP 的定义**。

## 2. 把模型、宿主、客户端、服务器分开

本例实际有这些角色：

| 角色 | 本例中是谁 | 具体做什么 |
| --- | --- | --- |
| 模型 | `.env` 指定的模型，例如兼容 OpenAI 接口的 DeepSeek | 根据用户问题和工具说明，输出工具名与参数 |
| Host，宿主 | `mcp_plugin.py` | 接收用户输入、请求模型、管理历史、检查权限、展示结果 |
| MCP client，客户端 | Python 里的 `ClientSession` | 与一个 server 建立协议会话、发现工具、发送调用 |
| MCP server，服务器 | `@playwright/mcp` 启动的 Node.js 进程 | 公布浏览器工具，并执行工具请求 |
| 具体执行组件 | Playwright 与新启动的浏览器 | 导航、查找页面元素、点击、输入 |

```text
用户输入
   |
   v
Python 宿主 ----------------------> 模型服务
   |                               返回 tool_calls
   | 检查工具名、参数、用户是否批准
   v
宿主内部的 MCP client
   | tools/call，通过 stdin/stdout 管道
   v
独立的 Playwright MCP server 进程
   | 调用 Playwright
   v
独立浏览器 -> 网页
   |
   +-- 操作结果沿原路回来 -> 宿主写入历史 -> 模型组织回答
```

关键区别：

1. **模型输出调用意图，不直接控制操作系统。** Python 收到 `tool_calls` 后，才决定是否执行。
2. **MCP 客户端通常是宿主程序里的一部分，不一定是另一台服务器。**
3. **MCP server 不一定需要大模型。** 本例的 Playwright server 不需要模型 API key。
4. 不使用模型也能调用 MCP。真实版的 `list` 和 `call` 就是在证明这一点。

这里把真实版命名为 `mcp_plugin.py`，意思是“为本教程的 agent 接入外部能力”。
MCP 没有规定所有产品都使用同一种 plugin 安装包；它也不会自动安装成 Codex 或 VS Code 的插件。
我们复用已经存在的 Playwright server，不再用 Python 重写一套浏览器服务。

## 3. 是不是需要两个端口？

### 3.1 本地 stdio：不需要给 MCP 开 TCP 监听端口

`stdio` 是标准输入/标准输出。它们不只用于键盘和屏幕，也能由父进程接成管道。

本例由 Python 启动 Node 子进程：

```text
Python 写入管道 -> Node 的 stdin：请求
Python 读取管道 <- Node 的 stdout：响应和通知
                       stderr：诊断日志
```

所以，本例不需要启动 `localhost:8000` 这样的 MCP HTTP 服务。
双方仍然是不同进程，只是使用操作系统管道交换消息。
服务器的 stdout 必须留给协议消息，普通调试日志应写 stderr，否则会破坏消息解析。

“没有 MCP TCP 端口”不等于整个程序离线：模型 API、浏览器访问网站仍可能走网络。
Playwright 与浏览器之间也有自己的控制通信，那是另一层实现细节。

### 3.2 远程 Streamable HTTP：连接一个已经运行的 MCP 地址

例如官方 Notion MCP 的地址是 `https://mcp.notion.com/mcp`。
宿主连接该地址，而不是在本机启动 Notion 的服务器进程。

```text
宿主里的 HTTP MCP client
   -> HTTPS /mcp
远程 MCP server
   -> Notion 业务能力
```

HTTP 模式由服务器监听一个地址/端口。客户端建立连接时也会有临时源端口，但你不需要手动部署“两个监听端口之间的中转服务”。
Streamable HTTP 使用 HTTP 请求交换消息，必要时使用 SSE 持续传送事件；SSE 可以理解为服务器持续往一条 HTTP 响应流里发送消息。
本例**只实现 stdio**，不能把一个 URL 填进 `--server-command` 就当成 HTTP 客户端。

参考：[MCP 传输规范](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)、[Notion 官方连接说明](https://developers.notion.com/guides/mcp/get-started-with-mcp)。

## 4. 真正的 MCP 消息长什么样？

下面按本例使用的经典会话流程说明。版本号是我们实测协商到的 `2025-11-25`，不是宣称它永远是最新版本。

### 4.1 先知道连接谁，再握手

用户或管理员先配置可执行程序和参数，例如：

```text
npx -y @playwright/mcp@0.0.84 --headless --isolated
```

`npx` 是 npm 提供的包执行命令；第一次可能下载这个指定版本，然后运行它。
它运行的是第三方程序，因此必须先确认来源可信，不能让模型随意拼接下载命令。

连接后，SDK 先发 `initialize`：

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "initialize",
  "params": {
    "protocolVersion": "2025-11-25",
    "capabilities": {},
    "clientInfo": {"name": "lesson-client", "version": "1.0"}
  }
}
```

服务器返回自己支持的协议版本、身份和能力；客户端确认可以使用后，再发 `notifications/initialized`。
**能力协商**就是先确认对方支持什么，例如是否提供 tools，而不是猜测每个服务器都具有相同功能。
Python SDK 的 `await session.initialize()` 包办了这段流程，我们不手写消息编号和管道解析。

参考：[初始化与生命周期](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle)。

### 4.2 发现工具：拿到的是说明书，不是实现源码

客户端请求：

```json
{"jsonrpc":"2.0","id":2,"method":"tools/list"}
```

服务器返回的工具大致如下，省略了其他工具和分页信息：

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "result": {
    "tools": [{
      "name": "browser_navigate",
      "description": "Navigate to a URL",
      "inputSchema": {
        "type": "object",
        "properties": {"url": {"type": "string"}},
        "required": ["url"]
      }
    }]
  }
}
```

`inputSchema` 是参数的结构说明：这里要求一个对象，对象中要有字符串 `url`。
发现工具不是自动寻找互联网上所有 MCP server，只是询问**已经连接的那个 server**有哪些工具。

宿主再把它转换成模型接口需要的格式：

```text
MCP 的 name         -> 模型看到的 function.name
MCP 的 description  -> function.description
MCP 的 inputSchema  -> function.parameters
```

本例把名字变成 `mcp__playwright__browser_navigate`，同时保留“别名 -> 原名”映射。
前缀方便多个服务的工具共存，但只是宿主约定，不是 MCP 规范强制的名字。

### 4.3 模型选择工具后，宿主才真正发调用

假设模型输出的 `tool_calls` 包含：

```json
{
  "id": "call_abc",
  "type": "function",
  "function": {
    "name": "mcp__playwright__browser_navigate",
    "arguments": "{\"url\":\"https://example.com\"}"
  }
}
```

宿主依次做四件事：找到原名、解析 JSON、校验参数、询问用户是否允许。
批准后，`session.call_tool(...)` 发出的 MCP 消息类似：

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "tools/call",
  "params": {
    "name": "browser_navigate",
    "arguments": {"url": "https://example.com"}
  }
}
```

这里有两套不同的编号：

- `call_abc`：模型对话中的调用编号，宿主写回 `role="tool"` 时使用。
- `3`：MCP 通信中的请求编号，SDK 用它把响应与请求配对。

二者没有必要相同。协议请求 id 也不是防止重复提交订单的“幂等键”。

### 4.4 服务器结果回来后，还要再请求模型

MCP 返回的结果通常有 `content` 内容块，以及 `isError` 错误标志。
某些工具还会有 `structuredContent`，即可以按字段读取的结构化结果。

宿主把结果转换成文本，加入历史：

```python
messages.append({
    "role": "tool",
    "tool_call_id": "call_abc",
    "content": "工具执行结果……",
})
```

前面那条含 `tool_calls` 的 assistant 消息也必须保留。
接着把新的历史发给模型，模型才知道操作结果，决定继续调用还是回答用户。

**MCP server 通常不会替宿主完成“再问模型、整理答案”的循环。**
协议通信循环和 agent 推理循环不是同一个东西。

参考：[MCP tools 规范](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)、[Chat Completions 消息格式](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)。

## 5. 原来的 code.py 模拟了什么，省略了什么？

原版 `MCPClient.register()` 直接保存 Python 列表和 handler；`call_tool()` 直接调用 Python 函数。
它展示了能力接入的结构，但没有启动真正的 MCP server，也没有 `initialize`、JSON-RPC 或传输层。
把一个类命名成 `MCPClient`，不代表它已经实现 MCP。

教学版新文件进一步去掉前面章节的团队、任务、worktree、模型配置等内容，只剩：

```text
初始工具池只有 connect_mcp
   -> 模拟模型请求 connect_mcp("docs")
   -> 取得 docs 提供的 search 工具说明
   -> 重新组装工具池
   -> 模拟模型请求 mcp__docs__search(query="MCP")
   -> 查路由，交给 docs 的 search 实现
   -> 结果写回历史
```

预先写好的 `simulated_calls` 是演示数据，不是真模型输出；这样不需要密钥，流程也不会随机变化。

原版的两个表述还要补充边界：

- 名字中的特殊字符换成下划线，不等于“已经防止注入”。`a.b` 和 `a/b` 反而可能同时变成 `a_b`，所以新示例显式拒绝冲突。
- 工具动态变化，不代表一定必须禁用缓存。需要的是让旧缓存失效，或把工具版本纳入缓存键；直接不缓存只是简单方案。

## 6. 真实版为什么仍然比较短？

浏览器实现由官方 Playwright MCP 提供；协议编解码与会话管理由官方 Python MCP SDK 提供。
我们只写宿主需要承担的部分，不把底层协议重新实现一遍。

按下面的顺序看 `mcp_plugin.py`：

| 函数 | 你可以直接理解为 |
| --- | --- |
| `server_parameters()` | 确定要启动哪个已信任的程序，不使用 shell 拼接 |
| `open_mcp()` | 启动子进程、接管道、握手；结束时清理连接 |
| `discover_tools()` | 分页拿完工具说明，不漏掉后面的页 |
| `assemble_tool_pool()` | 选出允许的工具，翻译成模型接口格式，记住原名 |
| `parse_arguments()` | 检查参数 JSON 和 schema，错误时不执行 |
| `result_text()` | 转换结果，保留错误标志，限制送给模型的文本长度 |
| `run_turn()` | 模型提议 -> 人工批准 -> MCP 调用 -> 回填结果 -> 再问模型 |
| `chat()` | 读取 .env，保留多轮历史，等待下一句用户输入 |

`async`/`await` 在这里不是“启动多个模型同时思考”：

- 等模型网络响应时，事件循环还能处理 MCP 消息。
- 等 MCP 执行浏览器操作时，Python 不需要一直占住线程做空循环。
- 等用户键盘输入时，`asyncio.to_thread(input, ...)` 把阻塞的输入放进工作线程。
- 工具仍按返回顺序逐个执行。不要把“使用 async”理解成“所有工具默认并行”。

同一次 `chat` 进程中，MCP 会话和浏览器可以跨用户输入保留。
模型答完后并没有在后台持续思考，也没有本章自动添加的 cron 调度。
浏览器页面本身的 JavaScript 可能继续运行，这与模型是否被再次调用是两回事。

## 7. 怎么运行？

以下命令从 `learn-claude-code` 根目录执行。Python 使用 3.10 或以上版本。
Python 依赖锁定到本次验证使用的 SDK v1 API；不要直接改成 SDK v2 却仍照抄 `ClientSession` 示例。
`mcp==1.30.0` 是教学兼容基线，不是在声称它是最新主版本。

### 7.1 先运行零依赖教学版

```bash
python s19_mcp_plugin/mcp_teaching.py
```

### 7.2 安装真实版依赖

需要 Node.js 18+、npm/npx，以及本机 Chrome。Node/npm 和浏览器是独立依赖，`pip install mcp` 不会替你安装它们。

```bash
python -m venv s19_mcp_plugin/.venv
s19_mcp_plugin/.venv/bin/python -m pip install -r s19_mcp_plugin/requirements_mcp.txt
node --version
npx --version
```

Windows 使用虚拟环境的 `Scripts/python.exe`，替换上面的 `bin/python`。
如果没有 Chrome，可以先按 [Playwright MCP 安装文档](https://playwright.dev/mcp/installation)准备浏览器；本例不会在模型请求下自动安装软件。

### 7.3 先列工具，不涉及模型

```bash
s19_mcp_plugin/.venv/bin/python s19_mcp_plugin/mcp_plugin.py list
```

它会真实启动服务器、握手、打印协商版本和服务器返回的工具名、用途及参数结构，然后退出。
不需要 `.env`，也不会自动打开浏览器访问网站。
第一次 `npx` 运行可能需要联网下载包；生产部署应预先安装并锁定依赖，而不是每次临时下载。

### 7.4 直接调用一次工具，仍不涉及模型

```bash
s19_mcp_plugin/.venv/bin/python s19_mcp_plugin/mcp_plugin.py call browser_navigate \
  --arguments '{"url":"https://example.com"}'
```

这是用户明确点名执行的命令，不会再弹出模型模式的确认提示。
`call` 可以调用 `list` 中的全部工具；不要把不可信文本拼接进它。
每次启动 `call` 都是新进程、新会话，**下一条独立的 `call browser_snapshot` 不会接着查看上一次已退出的浏览器**。
连续导航、读取、点击请在同一次 `chat` 中进行，或参照集成测试在同一个 `open_mcp()` 中调用。

### 7.5 接入之前的模型配置

沿用项目根目录 `.env` 的三个变量，不需要新增一种模型配置方式：

```dotenv
LLM_API_KEY=你的模型服务密钥
LLM_BASE_URL=你的OpenAI兼容接口地址
LLM_MODEL_ID=支持工具调用的模型ID
```

`LLM_BASE_URL` 可省略以使用 SDK 默认地址；使用 DeepSeek 等兼容服务时填对应地址。
模型密钥与 Notion OAuth token 不是同一回事，Playwright MCP 本身不需要这三个变量。

```bash
s19_mcp_plugin/.venv/bin/python s19_mcp_plugin/mcp_plugin.py chat
```

可以输入：`打开 https://example.com，读取页面正文，告诉我页面标题和主要内容。`

你会看到模型提出的工具名和完整参数。输入 `y` 才执行这一项，其他输入表示拒绝；`exit` 退出对话。
模型模式只开放导航、快照、点击、输入、按键和关闭六类工具。
不要使用带私人登录态的日常浏览器，本例使用 `--headless --isolated` 新建独立浏览器环境。
这隔离的是浏览器会话状态，不是操作系统级的安全沙箱。

**特别注意快照：** 固定版本的服务器在导航后默认会把自动快照保存为文件，返回一个路径。
模型收到路径，不等于已经读到了文件内容。
本例设置 `--snapshot-mode none`，导航后由模型显式调用 `browser_snapshot`；不提供 `filename` 时，页面快照正文直接进入工具结果。
文本超过 16000 个字符时会明确截断，不保存完整结果索引，也没有 s08 的上下文压缩。

### 7.6 本机没有 npx 时，使用已经安装的服务器

也可以通过 `--server-command` 传入 JSON 数组，直接启动现成的 Node 与 `cli.js`。
这是用户设置，不是开放给模型的工具。路径含空格时也不需要自己拼 shell 引号。

这次验证已在本机 `.cache/s19-mcp/node` 安装了固定版本，可直接运行：

```bash
s19_mcp_plugin/.venv/bin/python s19_mcp_plugin/mcp_plugin.py \
  --server-command '["/Users/dominic/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node","/Users/dominic/my_study/learn-claude-code/.cache/s19-mcp/node/node_modules/@playwright/mcp/cli.js","--headless","--isolated","--image-responses","omit","--no-webmcp","--snapshot-mode","none"]' list
```

把最后的 `list` 改成 `chat`，就进入模型模式。
这些绝对路径是本机环境，不是仓库的通用安装路径；在另一台电脑上请使用前面的 npx 方式或替换路径。
自定义命令会完整替换默认命令，因此需要自行保留隔离等启动参数。

## 8. 如果换成 Notion，哪些不变，哪些要改？

不变的是：初始化、能力发现、`tools/list`、`tools/call`、参数 schema 和结果处理的大方向。
不是把 `browser_navigate` 换个名字就完成了接入。

通常还要改：

1. 连接配置：官方托管 Notion MCP 使用远程 HTTP 地址，不是本例的本地 stdio 进程。
2. 登录授权：宿主需要支持它的 OAuth 流程，获取并安全保存、刷新访问凭据。
3. 业务权限：用户是否能访问目标 workspace、页面，是否允许更新或删除内容。
4. 工具池：发现 Notion 的实际工具，重新选择白名单、命名空间和审批规则。

OAuth 在这里可以理解为“让用户在授权页面同意访问，再给应用有限权限的凭据”，不是把 Notion 密码塞进 prompt。
不同 Notion MCP 实现的登录方式可能不同，不要把传统 Notion integration token 当成官方托管 MCP OAuth 的通用替代品。

本例实际接入并验证的是 Playwright，没有替你连接 Notion 账户。
参考：[Notion 官方 MCP 接入](https://developers.notion.com/guides/mcp/get-started-with-mcp)。

## 9. 生产级 MCP 应该多做什么？

“真的使用 MCP”和“适合生产部署”是两个维度。
真实协议接通说明接口能互相理解；生产级还要考虑用户权限、业务后果、失败恢复和长期维护。
也不是功能越多越生产级：单用户本地工具和多租户远程服务，要求不同。

### 9.1 身份与权限：不能只信任模型，也不能只信任客户端

**认证**回答“你是谁”，**授权**回答“你能操作哪些对象”。
例如一个用户能登录，不代表他就能读取另一个公司的 Notion 页面。
远程服务器应在每次业务调用时检查用户、租户、对象和所需权限；客户端弹确认不替代服务端授权。

`readOnlyHint`、`destructiveHint` 等 annotations 是服务器提供的提示，不是经过验证的安全保证。
不能因为工具自称只读就跳过权限判断。工具描述、网页正文和返回结果也可能含误导模型的指令。

本例只做“六类工具白名单 + 每次确认 + 不把整个环境变量交给子进程”。
生产环境还应限制可执行程序、浏览器访问目标、文件范围，并对提交表单等行为实施业务审批。
浏览器的 `--isolated`、进程的 `cwd` 都不等于 OS 沙箱；服务器包本身必须可信。

### 9.2 业务服务端：标准接口后面仍需要认真实现

假设你要提供一个真正的 `deploy_service`：

```text
接到 tools/call
  -> 校验 service/environment 等参数
  -> 确认调用者有部署这个项目的权限
  -> 检查是否重复请求、当前是否允许部署
  -> 调用真实部署系统
  -> 保存部署任务编号
  -> 返回明确状态与可查询的任务编号
```

MCP 处理的是标准消息。参数对应哪个服务、谁能发布、如何查状态和回滚，仍然属于业务实现。
服务端同样需要参数校验，不能因为“客户端应该已经校验过”就省略。

### 9.3 错误恢复：超时不等于没执行

至少区分以下情况：

| 情况 | 含义 | 合理处理 |
| --- | --- | --- |
| 参数不合法 | 尚未执行就能发现 | 返回校验错误，让调用方修正 |
| 工具业务失败，`isError=true` | 请求送到了工具，但业务没有按预期完成 | 保留错误原因，不伪装成成功 |
| 协议错误 | 方法不支持、消息格式不符等 | 修复协议或配置，不盲目重复业务操作 |
| 超时、断线 | 客户端没有拿到结果 | 先确认是否已执行，再决定重试 |

**幂等**指同一个业务操作重复请求，不会多产生一次副作用。
例如“设置状态为关闭”比较容易做幂等；“再支付 100 元”默认不是。
重要写操作需要业务幂等键、去重记录或可查询的操作编号，不能把 JSON-RPC id 当成已经实现了幂等。
读操作也不应无限重试：限制次数、逐渐延长等待，避免把下游压垮。

取消等待、杀掉客户端、关闭连接，都不保证远端业务已取消，更不保证已经回滚。
本例遇到 MCP 调用异常就结束会话，不自动重试，也不声称刚才的操作一定没有发生。

### 9.4 长期运行：会话、并发与隔离

本地服务要管理子进程的启动失败、stderr 日志、退出和残留进程。
远程服务要管理连接断开、会话过期、用户凭据过期，以及重连后哪些状态仍存在。
生产 HTTP 部署还需要合适的 TLS、认证、Origin 校验和监听地址；不要把无鉴权的本机工具随意暴露给公网。

多个用户使用浏览器时，不能默认共享同一套 cookie、标签页和登录态。
需要按用户/会话隔离浏览器 context，并限制同时运行的会话数。
有先后关系的操作不能乱并行，例如“导航到订单页”和“点击支付”必须按正确状态执行。

### 9.5 数据与可观测性：知道失败发生在哪里

**可观测性**是发生故障时能查清楚原因，不是只看到一行“Error”。
可以记录：宿主会话、用户、模型调用 id、MCP 请求 id、工具名、耗时、错误类型、下游任务编号。
记录时应隐藏 token、密码、私人页面正文；终端日志还应处理不可信控制字符。

大结果要限制接收大小、解析耗时和给模型的 token；仅在结果到手后切掉字符串，不是防内存耗尽的完整方案。
图片、音频和资源链接需要相应处理，不应假装它们都是文字。
需要落盘时提供受控路径、保留策略和可读取的引用；返回文件名本身不会让模型自动读到内容。

### 9.6 版本与工具更新：可复现，才能排查变化

锁定 SDK、服务器包及必要的传递依赖，升级前跑协议和业务回归测试。
生产安装还应使用依赖锁文件/完整性校验、受信任包源，而不是把 `@latest` 当作稳定部署。

工具列表可能分页，也可能在连接期间变化。
如果服务器支持工具列表变更通知，宿主应该重新发现工具、更新路由并使相关缓存失效。
本例只在连接后发现一次；运行中新增工具需要重启，避免为了演示加入一整套热更新系统。

MCP 还有 resources（可读取的数据资源）、prompts（可复用提示模板）等能力。
不是每个 server 都必须实现所有能力；本例只使用 tools，不代表 tools 就是整个 MCP。

安全参考：[MCP 安全实践](https://modelcontextprotocol.io/docs/2025-11-25/tutorials/security/security_best_practices)、[Playwright MCP 配置边界](https://playwright.dev/mcp/configuration/options)。

## 10. 这个例子的验证范围与限制

本次在 Python 3.10、MCP SDK 1.30.0、Playwright MCP 0.0.84 和本机 Chrome 上验证：

- 教学版可直接运行，无密钥、无网络请求。
- 真实握手协商到 `2025-11-25`，发现 25 个工具。
- 真实浏览器打开临时本机网页，获取快照、点击按钮、再获取快照确认文字已变化。
- 工具别名、参数检查、拒绝执行、分页、错误、执行预算和消息配对有离线测试。
- 真实 OpenAI SDK 的请求序列化使用 `httpx.MockTransport` 验证，**没有调用真实模型服务**，不代表已验证你的 key 或模型兼容性。

本例不是生产系统：没有远程 HTTP/OAuth、多用户隔离、全面网络沙箱、审计存储、长会话压缩、自动重连或业务幂等层。
输入会话保存在内存里，退出不保存；长对话可能超出模型上下文，需要另行接入 s08 等机制。

运行离线测试：

```bash
s19_mcp_plugin/.venv/bin/python -m unittest discover -s tests -p test_s19_mcp_plugin.py -v
```

默认跳过真实浏览器测试。下面显式开启它，只使用本机测试页面，不访问私人浏览器资料，也不调用真实模型：

```bash
S19_MCP_TEST_COMMAND='["npx","-y","@playwright/mcp@0.0.84","--headless","--isolated","--image-responses","omit","--no-webmcp"]' \
  s19_mcp_plugin/.venv/bin/python -m unittest discover -s tests -p test_s19_mcp_plugin.py -v
```

本机没有 npx 时，用第 7.6 节的 Node/cli.js JSON 数组替换该环境变量。
测试需要允许绑定临时的 `127.0.0.1` 端口和启动浏览器；测试结束会关闭页面、MCP 会话和临时 HTTP 服务。

最后可以用这条检查自己的理解：

> 去掉模型后，MCP 仍然能被程序调用；去掉具体服务器的业务实现后，只剩协议，浏览器不会自己动起来。
