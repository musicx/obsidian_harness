# work_sheet

一个人和一组 agent 共同维护的 Obsidian 知识库。你在 Obsidian 的 `inbox/` 里用自己的话提要求、给材料，引擎把它交给合适的 agent 去做：收录文章、对照你们现有的做法提修改建议、联网扩展一个方向、整理项目材料、写汇报和综述、回答问题。结果以 Markdown 写回 vault，每一条都带出处。

工作方式很简单：vault 是一个 git 仓库，**你提交并同步就是"发送"**；引擎巡检这个仓库，发现新内容就调度任务、启动 agent、检查产出、提交回去；Obsidian 自动把结果拉下来。

设计与需求见 [docs/design.md](docs/design.md) 和 [docs/user-stories.md](docs/user-stories.md)。

## 目录

- [准备](#准备)
- [初始化 vault](#初始化-vault)
- [安装引擎](#安装引擎)
- [运行](#运行)
- [日常使用](#日常使用)
- [部署到服务器常驻](#部署到服务器常驻)
- [多台机器协作](#多台机器协作)
- [数据分析](#数据分析)
- [外部简报导入](#外部简报导入)
- [查看用量](#查看用量)
- [排查问题](#排查问题)
- [开发](#开发)

## 准备

| 需要 | 说明 |
|---|---|
| git、[uv](https://docs.astral.sh/uv/) | 引擎用 Python 3.13，由 uv 管理 |
| Node.js 22 以上 | 抓取网页正文用到 `npx defuddle`；pi 也需要 |
| [pi](https://www.npmjs.com/package/@earendil-works/pi-coding-agent) | 执行 agent 的 CLI：`npm install -g @earendil-works/pi-coding-agent`，并登录至少一个模型服务 |
| DeepSeek API（可选） | 只有"联网搜集资料"用到：搜索和打开网页走 DeepSeek 的 Search / Fetch API，用 pi 里登录的 DeepSeek key；没有它其余功能照常 |
| Obsidian 1.14 以上 | 看板和首页视图用到核心功能 Bases |
| 一个私有的 GitHub 仓库 | 用作 vault |

## 初始化 vault

1. **建仓库并打开。** 在 GitHub 上新建一个私有仓库，克隆到本地，在 Obsidian 里用"打开本地仓库"选这个目录。vault 的根目录必须就是 git 的根目录（`.obsidian/` 和 `.git/` 在同一层）。

2. **装 obsidian-git。** 在 Obsidian 的第三方插件里安装并启用 [Git](https://github.com/Vinzent03/obsidian-git) 插件，然后把它设成"手动提交、自动拉取"。可以在插件设置里改，也可以直接写 `.obsidian/plugins/obsidian-git/data.json`：

   ```json
   {
     "autoSaveInterval": 0,
     "autoPushInterval": 0,
     "autoPullInterval": 2,
     "autoPullOnBoot": true,
     "pullBeforePush": true
   }
   ```

   这样只有你执行 **Git: Commit-and-sync** 时才会提交和推送，引擎写回的结果每两分钟自动拉取一次。建议给这条命令设一个快捷键。

3. **设置附件目录。** 设置 → 文件与链接 → 附件默认存放路径，填 `inbox/attachments`。拖进笔记的文件会存到这里，并且能被引擎当作材料读取。

4. **忽略界面状态文件。** 在 vault 根目录建 `.gitignore`：

   ```
   .obsidian/workspace*.json
   .DS_Store
   ```

5. **做第一次提交。** 随便建一页笔记，执行 Commit-and-sync，让远端有第一个提交。

6. **让引擎铺好骨架。** 引擎第一次巡检时会把 [vault-template/](vault-template/) 里的文件补进 vault：首页 `Home.md`、`views/` 下的视图、任务看板 `tasks/board.base`、主题表 `wiki/topics.md` 和各个目录。它只补缺失的文件，不覆盖已有的。

之后 vault 的结构是：

```
Home.md        首页：用法、等你处理的事、项目、最新简报、最近收录
inbox/         你的入口，一页一个话题
raw/           原始材料的存档，只增不改
wiki/          知识库：来源摘要、概念、实体；目录和日志由引擎生成
practice/      "我们的做法"，归你所有，系统只提建议
proposals/     系统对 practice/ 的修改建议
projects/      项目记录：现状、日志、决策、汇报
outputs/       答案、综述、阶段简报
analysis/      数据分析笔记
datasets/      数据字典
briefings/     外部流水线产出的日报周报（可选）
tasks/         任务笔记和看板
```

## 安装引擎

```bash
git clone <本仓库地址> work_sheet
```

```bash
cd work_sheet && uv sync
```

```bash
cp .env.sample .env
```

编辑 `.env`，至少填这几项：

| 变量 | 填什么 |
|---|---|
| `WORKSHEET_VAULT_REMOTE` | vault 仓库的地址；这台机器要能推送到它 |
| `WORKSHEET_SMALL_MODEL` | 调度者用的模型，便宜的小模型即可 |
| `WORKSHEET_STRONG_MODEL` | 执行任务用的模型 |
| `WORKSHEET_WEB_MODEL` | 联网搜集用的模型 |
| `WORKSHEET_ENGINE_ID` | 这台机器在 `config/engines.toml` 里的名字，见[多台机器协作](#多台机器协作) |
| `WORKSHEET_TIMEZONE` | 可选；引擎所在机器和你不在同一时区时填你的时区 |

**只有一台机器时**：仓库里的 `config/engines.toml` 登记了两台机器作为示例。把它改成只留一个 `role = "coordinator"` 的条目（或直接删掉这个文件），并把 `WORKSHEET_ENGINE_ID` 设成那个条目的名字（删掉文件时不用设）。否则这台机器会把自己当成只执行、不调度的执行引擎，什么都不会发生。

模型名用 `pi --list-models` 能看到的 `provider/model` 形式。`.env` 里不放密钥，模型的凭证由 pi 自己保存（在 pi 里执行 `/login`）。

模型怎么调用定义在 [config/runners.toml](config/runners.toml)：角色只写 `small`、`strong`、`web` 这样的逻辑名，这个文件把逻辑名对应到具体的命令行。想换一个 CLI 或加参数，改这里；想给主模型配一个失败时顶上的备用模型，给 runner 加一行 `fallback = "另一个 runner 的名字"`。

## 运行

执行一轮巡检：

```bash
uv run worksheet tick
```

在前台持续巡检，默认每 60 秒一轮，Ctrl-C 停止：

```bash
uv run worksheet run
```

没有新内容时，一轮巡检只做一次 `git fetch`，不调用任何模型。

## 日常使用

1. 在 `inbox/` 新建一页，写想要什么。材料直接贴链接，或把文件拖进这一页，或写文件路径。
2. 执行 **Git: Commit-and-sync**。
3. 引擎处理后，这一页下面会出现任务块：先是已接单，完成后是结果摘要和产出页面的链接；系统有疑问也写在这里。
4. 想追问、提修改意见或回答它的问题，在任务块**下面**接着写，再同步。新的事情另开一页。

可以这样说：

| 你写 | 系统做 |
|---|---|
| 把这篇文章收进知识库：〈链接〉 | 存档原文，写来源摘要，把内容并入已有的概念页 |
| 对比我们现有的评审做法，看哪里要升级：〈链接〉 | 写一页逐条的修改建议；你回复"采纳 1、3"后才改你的文档 |
| 记一下我们现在做 X 的方式：…… | 整理成一页"我们的做法"，只用你说过的话 |
| 围绕这篇文章里的 X，找找相关资料 | 联网搜集来源，采集后并入知识库 |
| 新项目 A，这是 PRD：〈路径〉 | 建项目档案 |
| A 项目这周的周会纪要：〈路径〉 | 更新项目现状、追加日志、记录决策；与旧记录矛盾时不覆盖，向你提问 |
| 生成 A 项目的最新汇报，给管理层 | 写一份带出处的 Markdown 汇报 |
| A 项目现在卡在哪？ | 依据项目记录回答 |
| 写一份关于 X 的综述，下周分享用 | 把知识库里相关内容串成一篇 |
| 这周有什么变化？ | 写一份阶段简报，列出等你处理的事 |
| 整理一下知识库 | 合并重复页面、补链接、处理挂着的分歧 |
| 最近一个交易日主力资金净流入最多的 5 只股票是哪些？ | 在有这份数据的机器上用只读查询回答，写成带完整 SQL 的分析笔记 |
| 把资金流相关的表梳理一下 | 摸清表的粒度、范围和坑，写进数据字典 |

`tasks/board` 是任务看板：把失败或阻塞的卡片拖回 `ready` 是重跑，拖到 `cancelled` 是取消。拖完同样要 Commit-and-sync。

## 部署到服务器常驻

[deploy/oracle/](deploy/oracle/) 是一套在 Linux 服务器上常驻运行的配置，可以照着改。它的要点是 **agent 以一个单独的无特权用户运行**：引擎用户持有 git 凭证，agent 用户持有模型密钥，agent 读不到引擎用户名下的任何文件。

```bash
# 1. 建 agent 用户
sudo useradd --create-home --shell /bin/bash wsagent

# 2. 装 Node 22+ 和 pi（略），然后拉代码
sudo mkdir -p /opt/worksheet && sudo chown "$USER" /opt/worksheet
git clone <本仓库地址> /opt/worksheet
cd /opt/worksheet && uv sync --no-dev

# 3. 配置：从样本开始改
cp deploy/oracle/env.sample .env && chmod 600 .env

# 4. vault 的副本只让引擎用户读
mkdir -p .state/vault/clone && chmod 700 .state/vault/clone

# 5. 以 agent 用户登录模型服务（在 pi 里执行 /login）
sudo -iu wsagent pi

# 6. 安装并启动服务
sudo cp deploy/oracle/worksheet.service /etc/systemd/system/
sudo systemctl enable --now worksheet
```

几点说明：

- 服务单元里写的运行用户是 `ubuntu`、目录是 `/opt/worksheet`，与你的环境不同就先改。
- 引擎用户需要免密 sudo，用来把运行目录交给 agent 用户再收回（[run-as-agent.sh](deploy/oracle/run-as-agent.sh)）。
- 引擎用户要能推送到 vault 仓库（部署密钥或令牌）。
- `.env` 里的 `WORKSHEET_RUNNERS_FILE` 指向 [deploy/oracle/runners.toml](deploy/oracle/runners.toml)，那里定义了主模型和备用模型。
- 更新代码：`git pull && sudo systemctl restart worksheet`。看日志：`sudo journalctl -u worksheet -n 50`。

### Mac 上常驻

[deploy/mac/com.worksheet.engine.plist](deploy/mac/com.worksheet.engine.plist) 是一个 launchd 用户级任务：登录后自动启动引擎，进程退出后 30 秒内重启。里面的项目路径和 `PATH` 是按一台具体机器写的，与你的环境不同就先改。launchd 不读 shell 配置，`PATH` 必须能找到 uv、git、pi 和 npx。

```bash
cp deploy/mac/com.worksheet.engine.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.worksheet.engine.plist
```

- 看状态：`launchctl print gui/$(id -u)/com.worksheet.engine | grep -E 'state|pid'`。
- 看日志：任务记录在 `.state/<workspace>/engine.log`，启动失败和崩溃信息在 `.state/launchd.log`。
- 更新代码后重启：`launchctl kickstart -k gui/$(id -u)/com.worksheet.engine`。
- 停止并取消常驻：`launchctl bootout gui/$(id -u)/com.worksheet.engine`。
- 正在执行的任务会被重启打断，下一轮记为失败；在看板上拖回 `ready` 即可重跑。

## 多台机器协作

数据在哪台机器上，任务就在哪台机器上跑。[config/engines.toml](config/engines.toml) 登记每台机器：

```toml
[engines.oracle]
role = "coordinator"            # 全局只有一个，应当常驻
description = "常驻服务器……"
runners = ["small", "strong"]
resources = ["oracle-clickhouse"]

[engines.mac]
role = "worker"
description = "个人电脑，不常开；可以联网搜集……"
runners = ["small", "strong", "web"]
resources = ["a-share-data", "local-files"]
```

每台机器在自己的 `.env` 里用 `WORKSHEET_ENGINE_ID` 说明自己是哪一个，然后照常运行 `worksheet run`。

- **协调引擎**负责调度、铺模板、生成目录和日志，并执行定给自己的任务。
- **执行引擎**只执行定给自己的任务。它没开机时，这些任务停在 `ready`，任务块里写着在等谁。
- 任务定给谁：某个操作需要的 runner 或资源只有一台机器有，就定给那台；你在请求里明说"在我电脑上处理"，以你说的为准；否则由协调引擎自己做。
- 材料由任务所在的机器采集，所以本机文件路径只有在任务定给那台机器时才读得到。

只有一台机器时，删掉 `config/engines.toml` 或只留一个 `coordinator`，其余不变。

## 数据分析

要让系统回答需要查数据库的问题，在有数据的那台机器上登记数据源：把 [config/datasources.sample.toml](config/datasources.sample.toml) 复制为 `config/datasources.toml`（不入库）并填好连接信息，再在 [config/engines.toml](config/engines.toml) 里给这台机器加一个同名的资源，写一句说明里面有什么数据——调度者靠这句说明判断问题该去哪个数据源。

目前支持 ClickHouse。查询是只读的：用只读账号，或让工具给每个请求强制带上只读参数；每次最多返回 200 行。agent 没有 shell，只能通过这个查询工具访问数据。分析笔记里贴出的每条 SQL 都必须是实际执行过的，引擎会逐条核对。

产出在 `analysis/`（分析笔记）和 `datasets/`（数据字典，一表一页，越用越全）。

## 外部简报导入

别的流水线定期产出的日报、周报，可以让引擎整批导入 vault 的 `briefings/`。在一个 `feeds.toml` 里登记来源目录（样例见 [deploy/oracle/feeds.toml](deploy/oracle/feeds.toml)），用 `WORKSHEET_FEEDS_FILE` 指向它。来源是一批按日期命名的 JSON 文件，含 `date`、`title`、`md` 或 `html` 字段。简报只读、不自动并入知识库；想留下某一篇的内容，在 inbox 里把它的路径当作材料交给系统。

## 查看用量

每次调用模型的 token 用量和耗时记在两处：任务笔记的 `usage` 属性和"完成"那一行；以及引擎本机的 `.state/<工作区>/usage.jsonl`，一次调用一行，区分调度、任务、修正和备用模型。

## 排查问题

| 现象 | 原因与处理 |
|---|---|
| 同步后一直没有任务块 | 引擎没在运行，或文字写在了任务块上方（只识别最后一个任务块**下面**的新内容） |
| 任务一直是 `ready`，写着"等待某引擎执行" | 那台机器上的引擎没开 |
| `blocked`：材料采集失败 | 链接需要登录或依赖 JS 渲染；把文件下载后拖进那一页，再把卡片拖回 `ready` |
| `failed`：检查未通过 | 产出违反了写入范围或页面规范，原因写在任务块里；引擎已让 agent 修正过一次。拖回 `ready` 可重跑 |
| `failed`：运行中断 | 上次运行被打断；拖回 `ready` 重跑 |
| 日志里 `invalid dispatcher output` 后紧接着成功 | 调度者第一次输出不合规，已自动重问，无需处理 |
| 模型服务连不上 | 引擎不会建任务，下一轮自动重试；配了备用模型时会先换备用模型 |
| 看板或首页视图报错 | 确认 Obsidian 版本在 1.14 以上；`.base` 文件可以用 [vault-template/](vault-template/) 里的版本覆盖 |

## 开发

```bash
uv run pytest -q
```

测试用真实的 git 和假的 agent 脚本，不访问网络。改了路由规则、skill 的说明或调度模型后，用真实模型跑一遍路由评测：

```bash
uv run worksheet eval-routing
```

仓库结构：

```
src/work_sheet/     引擎
roles/              角色：能读什么、能写什么、几条底线
skills/             每种操作的规程，以及所有角色共用的写作规范
config/             runner、引擎名册、路由规则
vault-template/     vault 骨架
deploy/oracle/      服务器部署样例
evals/              调度者的路由评测用例
docs/               设计文档与用户故事
```

增加一种操作：在 `skills/` 下建一个目录写 `SKILL.md`，在 `metadata.role` 里写由哪个角色执行。调度者会自动看到它，凭它的说明决定何时使用。
