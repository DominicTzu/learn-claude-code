# s19 补充：Prompt、Context、Skill、MCP 与 CLI

这一节接在 `s19_mcp_plugin` 后面，但代码可以单独运行。
先把几个经常放在一起比较、其实不属于同一层的概念分开，再比较 MCP 与 CLI 的取舍。

## 1. 先记住它们分别解决什么问题

| 概念 | 主要回答的问题 | 具体例子 |
| --- | --- | --- |
| Prompt，提示词 | 这次希望模型做什么、遵守什么要求？ | “检查仓库状态，只报告，不修改文件” |
| Context，上下文 | 这次请求中，模型实际获得了哪些信息？ | 系统要求、用户问题、历史、已读取文件、工具说明与结果 |
| Skill，技能包 | 某一类任务应该按什么方法做？ | 一份代码审查流程，以及需要时读取的参考文件、运行的脚本 |
| MCP，模型上下文协议 | 不同程序怎样用统一规则公布和调用能力？ | 连接 Playwright MCP，发现并调用 `browser_snapshot` |
| CLI，命令行接口 | 一个程序如何通过命令名、参数、输入输出被使用？ | `git status --short`、`playwright-cli snapshot` |

**Prompt、Context、Skill 偏向“模型看什么、怎么做”；MCP、CLI 偏向“程序之间怎么调用能力”。**
Skill 中可以包含可执行脚本，所以这个划分不是说 Skill 永远只能有文字，而是在区分各自的主要职责。

它们可以同时出现在一个 agent 中，不需要五选一。

## 2. Prompt 和 Context：指令只是上下文的一部分

### 2.1 Prompt 不只是用户最后一句话

日常讨论里，prompt 有两种用法：

- 狭义：给模型的任务要求、约束、示例，例如 system prompt、user prompt。
- 广义：整次模型输入的统称，这时和 context 有重叠。

本节用狭义区分：**prompt 是你给出的要求；context 是模型这一次可用的输入信息整体。**

例如用户问“解释当前项目的变更”：

```text
Prompt：解释当前项目的变更，不要修改文件。

Context：
  上面的要求
  + 宿主提供的工作目录
  + 模型可用的工具说明
  + 已经读到的 git status / git diff 结果
  + 仍保留在这次请求里的历史
```

“完整 Git 仓库在磁盘上”不代表仓库已经全部进入 context。
“终端刚打印了结果”也不代表模型已经看到了它。
在本项目的 API 循环中，宿主需要把结果加入 `messages`，再发送模型请求。

类似地，文件路径不是文件内容，Skill 名字不是 Skill 正文，工具名不是工具的执行结果。
训练得到的知识也不等于当前请求的上下文；不要把“模型可能知道 Git 命令”误当成“模型知道当前仓库状态”。

### 2.2 Context engineering 不是把 prompt 写得更长

它主要是在决定：

- 此时需要读取哪些信息，哪些可以暂时不读？
- 哪些旧结果可以压缩，哪些证据需要保留原文或可追溯引用？
- 大结果能否先在程序中筛选，只把有关部分送给模型？
- 哪些是可信的操作要求，哪些只是网页、日志或工具返回的数据？

例如“日志有十万行，但只需要失败的十条”，先筛选后发送，通常比让模型阅读十万行更合适。
这件事既可以配合 CLI，也可以配合 MCP。

## 3. Skill：可复用的操作方法，不是自动执行的程序

一个常见的 Skill 目录长这样：

```text
review-changes/
  SKILL.md             什么时候使用，以及怎么完成审查
  references/          需要时再读的详细规范
  scripts/             可选的确定性辅助程序
```

例如 `SKILL.md` 可以规定：先查工作区状态，再读变更，再运行相关测试，最后报告问题与证据。

Skill 和一大段 prompt 的联系是：**Skill 被读取后，其中的指令仍会成为模型输入。**
区别在组织和加载方式：不必每次把所有业务流程都塞进系统提示词，而是先提供名称和简介，需要时再加载正文。
这叫渐进式披露，也就是“先给目录，选中后再给详细内容”。[官方 Skills 说明](https://learn.chatgpt.com/docs/build-skills)

但是不要把 Skill 理解成：

- 一装上就自动学会了新模型能力。它没有修改模型权重。
- 写了“运行测试”就会自己启动测试。仍需要模型请求工具，宿主实际执行。
- 有 `SKILL.md` 文件就一定被识别。宿主需要实现目录发现、匹配和加载。
- 里面写“不要删除文件”就有了安全隔离。权限还必须在执行层检查。

你在 s07 学的是 Skill 加载机制；本节不再实现一遍。
本节 `code.py` 为了聚焦 CLI，直接提供一小段 Git 操作说明，没有伪装成完整 Skill 系统。

## 4. MCP 与 CLI：调用的业务可以一样，入口不同

### 4.1 MCP 的路线

沿用上一节的浏览器例子：

```text
模型返回 browser_navigate 的工具调用
  -> 宿主校验、审批
  -> MCP client 发 tools/call
  -> Playwright MCP server
  -> Playwright 操作浏览器
  -> MCP 结果返回宿主
  -> 宿主把结果写入模型上下文
```

MCP 规定初始化、能力协商、工具发现、调用和结果格式等通信规则。
具体的浏览器能力由 Playwright MCP server 实现，不是协议本身产生的。
具体代码见上一节的 `s19_mcp_plugin/mcp_plugin.py`。

### 4.2 CLI 的路线

同一个业务也可以这样调用：

```text
模型返回 run_cli(argv=["playwright-cli", "snapshot"])
  -> 宿主校验、审批
  -> subprocess 启动 CLI 程序
  -> CLI 找到浏览器会话，执行快照操作
  -> 退出码、stdout、stderr 返回宿主
  -> 宿主把需要的结果写入模型上下文
```

CLI 没有要求宿主先执行 MCP 的 `initialize` 或 `tools/list`。
操作说明通常来自 `--help`、文档、Skill 或模型已有知识。

**CLI 不代表不再使用 tool calling。** 在我们的 Chat Completions 循环中，模型仍然通过 `run_cli` 这个工具申请执行。
变化的是宿主接到请求后的实现，不是模型突然能绕过宿主直接操作电脑。

### 4.3 CLI、Shell、终端、subprocess 不是一回事

| 名称 | 含义 |
| --- | --- |
| CLI | 程序提供的命令行接口，例如 Git 的命令和参数 |
| Shell | 解释命令语法的程序，例如 bash、zsh；处理管道、重定向、变量展开 |
| 终端 | 你看到输入输出的交互界面；不是所有 CLI 调用都需要打开终端窗口 |
| `subprocess` | Python 的进程管理库，用来启动程序、等待结果、接收输出 |

```python
subprocess.run(["git", "status", "--short"], shell=False)
```

这里直接启动 Git，不需要在 VS Code 的终端里模拟按键，也不需要先启动 zsh。
如果用 `shell=True` 执行整段字符串，才会额外引入 shell 解析。
所以 `shell=False` 下的 `|` 不会自动变成管道；参数数组中也不需要把含空格的参数再包一层引号。
参考：[Python subprocess 文档](https://docs.python.org/3/library/subprocess.html)。

### 4.4 不是只有 MCP 能保留会话

短命的是 CLI 的调用进程，不一定是它调用的后台服务。
当前 Playwright CLI 使用命名会话，多次命令可以操作同一个浏览器：

```bash
playwright-cli -s=lesson open https://example.com
playwright-cli -s=lesson snapshot
playwright-cli -s=lesson close
```

中间的后台进程和浏览器可以一直存在；最后的 `close` 才关闭这个会话。
因此“CLI 一定无状态，MCP 一定有状态”是错误的比较方式。
另一方面，CLI 也可能通过网络访问远端服务；它不等于“只会操作本地文件”。
参考：[Playwright CLI 会话说明](https://github.com/microsoft/playwright-cli/blob/main/README.md)。

## 5. 它们是怎么演化出来的？

先纠正一个容易误解的顺序：**CLI 远早于大模型，也远早于 MCP。**
不是先有 Prompt，再发明 Context，再由 Skill 升级成 MCP，最后进化成 CLI。

更合适的是看 agent 系统遇到问题后的设计演进，而不是把五个名词排成技术替代年表：

| 遇到的问题 | 常见做法 | 还没解决什么 |
| --- | --- | --- |
| 模型不知道任务要求 | 写清楚 prompt、约束和输出要求 | 不知道本次任务的实时事实 |
| 模型缺少项目资料 | 读取文件、检索资料、管理 context | 仍然不一定能执行操作 |
| 需要运行程序或改变外部状态 | 给 agent 提供函数工具、CLI、API、浏览器等执行入口 | 集成方式可能各不相同 |
| 多个服务都要重复接入不同 agent | 用 MCP 统一能力发现和调用协议 | 业务权限、工具数量、结果体积仍需管理 |
| 重复流程全堆进 prompt | 用 Skill 封装方法，按需加载 | 方法说明仍要依靠执行工具落实 |
| 工具说明与中间结果占用大量上下文 | 按需发现、CLI + Skill、代码执行、结果筛选 | 仍需校验、权限、版本和可靠性治理 |

这些方法会交叉组合，不是后一行把前一行淘汰。
本教程 s01 已经用终端命令执行任务，s07 加入 Skill，s19 再讨论 MCP，本身就说明它们不是单线替代关系。

## 6. 为什么现在一些 coding agent 更偏好 CLI？

### 6.1 先限定“更流行”这个判断

截至本次整理（2026-11-03），可以核实的具体例子是：微软 Playwright 官方向 coding agent 推荐 CLI + Skills，并强调工具说明和页面数据的上下文成本。
但这是特定项目的设计推荐，**不是整个行业的使用量统计，也不能据此说 MCP 已经过时**。
[Playwright 官方对比](https://github.com/microsoft/playwright-cli/blob/main/README.md)

下面是结合本教程结构做出的工程分析，不是市场占有率结论。

### 6.2 已经有执行环境，不必把每个现成命令重新包装一次

coding agent 通常已经需要读取项目和运行测试。
如果 Git、构建工具、测试工具都已经有 CLI，沿用这些入口能直接复用开发者和 CI 的工作流。

例如查本地仓库状态，直接运行 `git status` 就可以；未必需要专门建立 Git MCP server，再写一个 status 工具。
但在没有本地执行环境、需要远程业务授权的应用中，这个理由未必成立。

### 6.3 减少预先暴露的大量工具说明

一种朴素 MCP 接入会把很多服务器的全部工具 schema 都传给模型。
CLI 方案可以只暴露一个执行入口，让模型先读相关 Skill 或指定子命令的帮助，再执行需要的命令。

不过，少了 schema 不代表说明成本消失了：`--help`、Skill 正文、参数纠错也占 token。
把所有 CLI 手册一次性塞进 prompt，一样会使上下文膨胀。

### 6.4 可以在程序里组合、筛选，只返回结果

例如批量处理一百个文件：循环、条件判断和聚合可以由本地脚本完成，而不是每处理一项都让模型重新推理一遍。
CLI 通常容易被脚本和 CI 复用；支持 `--json`、字段选择、数量限制的命令，也方便只输出需要的数据。

这不是 CLI 独占能力。MCP 也可以由代码批量调用，在执行环境筛选结果后再交给模型。
Anthropic 的官方文章就介绍了这种 MCP + code execution 方式。[官方说明](https://www.anthropic.com/engineering/code-execution-with-mcp)

### 6.5 便于重放和排错，但也有代价

一条命令可以由人直接运行，容易复现参数、目录和退出码。
代价是宿主要处理工具安装、命令版本、路径、不同操作系统、交互式提示和输出格式变化。
如果 CLI 面向人类界面设计，输出充满颜色、进度条和表格，又没有机器输出模式，反而很难可靠解析。

### 6.6 不要把这些误当成 CLI 的保证

- **不保证更省 token：** 两条路线返回相同的大结果，仍然会产生相近的数据负担。
- **不保证更快：** 启动进程、加载运行时、下载包、额外读帮助都可能增加延迟。
- **不保证更安全：** 一个通用 shell 入口可能比一组受限 MCP 工具拥有更大的权限。
- **不保证更稳定：** 命令与输出格式会变；协议标准也不能替你保证业务行为不变。

前一节已经让 MCP 按需获取页面快照，本节 CLI 也这样做。
不能拿“精心筛选结果的 CLI”去对比“把全部结果都塞给模型的 MCP”，再把差距全部归功于接口名称。
本节不编造 token 节省比例，也不声称做了同任务性能基准。

## 7. 真正要选的是哪种组合？

| 场景 | 可以优先考虑 | 原因与条件 |
| --- | --- | --- |
| 本地代码仓库、测试、构建 | CLI + 简短操作说明或 Skill | 已有成熟工具，容易复用 CI 流程 |
| 一个服务要供多种宿主发现、调用 | MCP | 标准能力说明与调用方式减少重复适配 |
| 远程 SaaS、用户登录、组织权限 | 支持相应认证的 MCP 或 API 集成 | 需要服务端逐次授权；CLI 也能远程访问，但不是自动解决权限 |
| 高频、稳定、低延迟的业务路径 | 直接 SDK/API 也可能更合适 | 不必为了“agent 化”强行绕一层 MCP 或启动进程 |
| 不同工具组成的重复工作流 | Skill + CLI/MCP/SDK | Skill 负责方法，执行入口负责能力 |

例如一个完整任务可以同时用到全部五者：

```text
用户 prompt：修复问题并更新项目文档
  -> 宿主构造 context：问题描述、相关代码、工具说明
  -> 按需读取代码修改 Skill：开发与验证流程
  -> CLI：git、测试、构建
  -> MCP：访问远程知识库
  -> 工具结果进入新的 context
  -> 模型回答已完成内容和剩余问题
```

生产判断最后仍要落在：可用权限、失败后果、可追溯性、上下文成本与维护成本上。
不要只根据工具叫什么决定架构。

## 8. 代码一：用 Git CLI 看清基本链路

文件：`code.py`。只读 Git，不启动 MCP，不自动提交或修改仓库。

### 8.1 不需要密钥的 demo

从项目根目录运行，需要 Python 3.10+ 和 Git：

```bash
python s19_mcp_vs_cli/code.py demo
```

它真实执行三个预设命令：Git 版本、工作区简要状态、最近五条提交。
命令是程序预设的，没有调用模型，也不会生成假装来自模型的答案。

关键返回值类似：

```json
{
  "status": "ok",
  "exit_code": 0,
  "stdout": " M example.py\n",
  "stderr": ""
}
```

`exit_code` 是程序的退出码，`stdout` 是普通输出，`stderr` 是诊断输出。
上面只是格式示意，真实内容取决于你的仓库。
`git status --short` 中两个状态位置分别对应暂存区和工作区，例如 `??` 表示未跟踪文件。
参考：[Git status 格式](https://git-scm.com/docs/git-status)。

读取指定仓库：

```bash
python s19_mcp_vs_cli/code.py demo --repo /你的/仓库路径
```

### 8.2 让模型选择命令

```bash
python -m pip install -r s19_mcp_vs_cli/requirements.txt
python s19_mcp_vs_cli/code.py chat
```

沿用根目录 `.env` 的 `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL_ID`，模型需要支持 Chat Completions 工具调用。
不会替你更换模型；只有 `chat` 才导入 SDK 和读取密钥。

本机也可以直接复用上一节已安装依赖的独立环境，不需要重新安装：

```bash
s19_mcp_plugin/.venv/bin/python s19_mcp_vs_cli/code.py chat
```

注意：终端中的 `python` 和 VS Code 选择的解释器可能不是同一个。
如果遇到 `No module named openai`，请在正确环境安装依赖，或使用上面这个明确的解释器路径。

可以问：`检查当前仓库状态和最近五条提交，告诉我观察到了什么，不要修改文件。`

阅读顺序：

1. `ALLOWED_COMMANDS`：允许执行哪些完整命令。
2. `run_cli()`：检查白名单，启动 Git，接收退出码和输出。
3. `TOOLS`：模型只看到一个 `run_cli` 函数入口。
4. `agent_turn()`：模型提议、程序执行、回填结果、再次请求模型。

为了聚焦 CLI，本例没有通用 shell、动态 Skill 发现、任务系统或上下文压缩。
模型只能在五个固定只读命令中选择，不会把每一个 Git 子命令都包装成单独的模型工具。
所有输出都是“不可信的数据”；提交信息里即使出现操作指令，也不应成为新的权限依据。

## 9. 代码二：真实 Playwright CLI，对照前一节的 MCP

文件：`playwright_cli_demo.py`。Python 部分只用标准库，不需要模型密钥，也不导入 MCP SDK。
需要 Node.js 18+、npm/npx、本机 Chrome；使用固定的 `@playwright/cli@0.1.23`。

```bash
python s19_mcp_vs_cli/playwright_cli_demo.py
```

实际步骤是：

```text
生成本次唯一的会话名
  -> CLI open：启动独立无头浏览器、打开页面
  -> CLI snapshot：取得页面正文
  -> CLI close：关闭本次会话
```

调用之间使用同一个 `-s=会话名`。没有连接已有个人浏览器，不读取日常浏览器的登录态。
默认访问 `https://example.com`，也可以指定自己的网站：

```bash
python s19_mcp_vs_cli/playwright_cli_demo.py --url http://127.0.0.1:8000
```

如果要显式测试点击，可以加 `--click`，参数是已观察到的元素 ref 或明确的选择器。
它会真实点击，可能提交页面操作；不要把不可信页面文字拼进这个参数。
默认不点击。测试使用我们自己提供的本机按钮页面。

### 9.1 为什么 open 之后还要 snapshot？

本次固定版本的 `open --json` 会返回类似 `{"result":{"snapshot":{"file":"...yml"}}}` 的内容。
那只是文件引用，不是页面正文。
接下来显式调用 `snapshot --json`，才能拿到结构化的页面树。
示例没有用容易出错的正则表达式从打印日志中“猜”路径，而是请求 JSON 并使用 JSON 解析器。

这里不调用模型，因此结果仅显示在终端，不会自动进入任何模型上下文。
要让 agent 使用结果，就像 `code.py` 那样写回工具结果，再调用模型。

### 9.2 本机无需 npx 的运行方式

本次已把固定版本 CLI 安装在项目缓存中，没有全局安装或修改前一节 MCP 的配置。
当前电脑可以直接运行：

```bash
python s19_mcp_vs_cli/playwright_cli_demo.py \
  --cli-command '["/Users/dominic/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node","/Users/dominic/my_study/learn-claude-code/.cache/s19-cli/node/node_modules/@playwright/cli/playwright-cli.js"]'
```

这是本机路径，其他电脑请使用默认 npx 方式，或提供自己的程序路径。
`--cli-command` 是用户配置，不是给模型选择任意程序的权限。
第一次 npx 运行可能下载包；受控部署应预先安装并锁定依赖。

### 9.3 对比接口时不要误解底层实现

Python MCP 示例：调用 `session.call_tool(...)`，宿主管理 MCP 会话。
Python CLI 示例：调用 `subprocess.run(...)`，宿主使用命令行契约。
Playwright 的两种入口可以复用相同的内部浏览器实现；CLI 后面也可以有 daemon（后台服务进程）和内部通信。
所以本节对比的是**宿主使用的接口**，不是声称 CLI 内部绝对不能使用任何协议或服务。
参考：[Playwright 官方执行链说明](https://github.com/microsoft/playwright/blob/main/.claude/skills/playwright-dev/tools.md)。

## 10. 最容易出问题的地方

- `shell=False` 只是不解析 shell 语法，不代表命令本身无害。任意 Python、Git 配置参数或上传命令仍然可能有很大权限。
- `cwd` 只决定开始工作的目录，不限制程序能访问的其他文件。
- 子进程不应默认继承所有秘密环境变量；本例只传递少量基础变量。
- 输出应尽量选择结构化格式；不要假定所有 CLI 都有 `--json`，Git 也有自己的稳定机器格式。
- 只在读取完整 stdout 后截断，是上下文控制，不是防止进程耗尽内存的完整方案。本例未实现流式输出字节上限。
- 超时或进程被杀，不代表它产生的副作用已回滚。写操作重试仍需幂等设计。
- CLI 调用结束不代表后台会话结束；要关闭自己创建的会话，不要随手 `kill-all`。
- 长对话、返回日志、Skill 正文都会占用 context；CLI 不会自动处理压缩和记忆。

本例的只读命令白名单不是通用安全终端。把它扩展成任意 CLI 执行器前，要重新设计授权、隔离与审计。

## 11. 验证方式

默认测试使用临时 Git 仓库、假模型响应和模拟异常，不修改当前仓库：

```bash
python -m unittest discover -s tests -p test_s19_mcp_vs_cli.py -v
```

模型消息测试需要 `requirements.txt` 的依赖，以及 `httpx`（OpenAI SDK 的依赖）。
本机可将测试命令中的 `python` 替换为 `s19_mcp_plugin/.venv/bin/python`。
真实浏览器测试默认跳过。明确提供 CLI 启动命令才会开启：

```bash
S19_CLI_TEST_COMMAND='["npx","-y","@playwright/cli@0.1.23"]' \
  python -m unittest discover -s tests -p test_s19_mcp_vs_cli.py -v
```

没有 npx 时，使用第 9.2 节的 Node/CLI JSON 数组替换环境变量。
浏览器测试会打开本机临时网页、读取内容、点击按钮、确认内容变化并关闭会话。
模型测试使用模拟传输，不能据此声称真实模型服务、密钥或模型行为已经通过验证。

本次验证：13 项测试全部通过，包含真实 Git 仓库查询、真实 Playwright CLI 导航/快照/点击/关闭，以及模拟模型消息配对；Ruff 检查通过。未调用真实模型 API。

一句话总结：**Prompt 提出要求，Context 提供当次信息，Skill 组织方法，MCP 和 CLI 提供不同的能力调用入口；执行和权限始终由宿主及相应服务落实。**
