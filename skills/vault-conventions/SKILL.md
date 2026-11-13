---
name: vault-conventions
description: 这个 vault 里所有页面共用的写法：Obsidian 语法、各类页面的 frontmatter、命名、引用出处和标记分歧的方式。写任何 vault 页面前先读。
license: MIT
metadata:
  attribution: Obsidian 语法部分改编自 obsidianmd/obsidian-skills 的 obsidian-markdown（MIT，Steph Ango）
---
# Vault 写作规范

所有角色写进 vault 的页面都遵守这里的规则。引擎在导入时会机械检查其中带 ★ 的条目，不通过则整个任务作废。

## Obsidian 语法

- 链接：`[[页面名]]`、`[[页面名|显示文字]]`、`[[页面名#标题]]`。★ 只链接确实存在或本次新建的页面。
- 链接按**文件名**解析，不按别名解析。想显示别名就写 `[[页面名|别名]]`。
- 表格里的链接要转义竖线：`[[页面名\|显示文字]]`。表格前面留一个空行。
- `wiki/` 内的概念、实体、来源页用短名 `[[页面名]]`；链接 `raw/`、`practice/`、`proposals/`、`projects/`、`outputs/` 下的文件用完整路径，如 `[[practice/代码评审|代码评审]]`、`[[raw/2026/11/2026-11-13-xxx/source|原文]]`。
- frontmatter 里的链接必须加引号：`sources: ["[[页面名]]"]`。
- 提示框：`> [!note] 标题`、`> [!warning]`、`> [!question]`。本库另用两种：`> [!conflict]`（来源之间有分歧）和 `> [!outdated]`（说法已被更新）。
- 图用 Mermaid 代码块，不用外部图片。
- frontmatter 保持扁平：不要嵌套对象；列表用 `[a, b]` 或逐行 `- a`。日期一律 `YYYY-MM-DD`。只改你需要改的属性，其余原样保留。

## 页面类型与 frontmatter

★ `wiki/` 和 `practice/` 下的每个页面都要有 `description`：一句话说清"这里有什么、什么时候该读"，不超过 200 字。目录是引擎根据它自动生成的，写得含糊就等于找不到。

来源摘要 `wiki/sources/<材料标题>.md`

    ---
    type: source
    description: 这份材料讲什么；想了解什么时该读它
    raw: raw/2026/11/2026-11-13-xxx        # ★ 指向已存档的原文目录
    doc_date: 2026-07-25                   # 材料自身的日期，不知道就不写
    topics: [已登记的主题]
    concepts: ["[[本材料贡献内容的概念或实体页]]"]
    mentions: [提到但还不足以成页的概念名]
    aliases: []
    updated: YYYY-MM-DD
    ---

概念 `wiki/concepts/<名称>.md`、实体 `wiki/entities/<名称>.md`

    ---
    type: concept                          # 或 entity
    description: 一句话定义；什么时候该读
    aliases: [其他叫法、中文译名、缩写]
    topics: [已登记的主题]
    sources: ["[[来源摘要页]]"]            # ★ 至少一个
    updated: YYYY-MM-DD
    ---

我方文档 `practice/<做法名>.md`：`type: practice`、★ `owner: human`、`description`、`aliases`、`updated`。

建议页 `proposals/YYYY-MM-DD-<标题>.md`：`type: proposal`、`target: "[[practice/…]]"`、`source: "[[来源摘要页]]"`、`status: proposed | accepted | partially-accepted | rejected`、`created`。

★ `topics` 只能用 `wiki/topics.md` 里登记过的主题。确实需要新主题时，先在 `wiki/topics.md` 加一行 `- 主题名 — 一句话说明`，并在报告里说明。宁可少一个主题，不要为一篇材料新造一个。

## 不要碰的文件

`wiki/index.md`、各目录的 `_index.md`、`wiki/log.md`、`projects/_index.md` 由引擎生成。改它们会让整个任务作废。你只需要把每页的 `description`、`topics`、`aliases` 写好。

## 命名

- 文件名就是页面的名字。概念和实体用最通行的叫法；技术名词保留英文原文，写成首字母大写的标题形式（`Central Limit Theorem`、`Fat Tails`）。
- 文件名里不要出现 `/ \ : # ^ [ ] |`，不要带书名号或引号。
- ★ 新建页面的名字不能和已有页面的名字或别名相同或近似（忽略大小写、标点、单复数）。先查 `_context/names.md`；已有的就用已有的名字，把新叫法加进它的 `aliases`。
- 同名异义时加限定词：`Mercury (planet)`。
- ★ 整个 `wiki/` 里不能有两个文件名相同的页面（忽略大小写）。来源页的标题与某个概念页或实体页同名时，在来源页的名字后加上出处，如 `Kelly criterion (Wikipedia)`。

## 出处

- 每段陈述的末尾标出依据：`……（[[来源摘要页]]）`。一段综合了多个来源就都列上。
- 数字、日期、专有名词照原文写，不换算、不取整、不"润色"（原文写 42K 就写 42K）。
- 引号只用于逐字引用。转述不加引号。
- 你自己的归纳、推断或补全，标"（推断）"。材料里没有的内容不要凭记忆补。

## 分歧与更新

不要悄悄选边，也不要悄悄覆盖。

来源之间有分歧，并列写出：

    > [!conflict] 关于 <分歧点>
    > - [[来源 A]]（2026-03）：……
    > - [[来源 B]]（2026-10）：……
    > 目前无法判断哪个正确 / 较新的是 B，依据是……

新材料更新了旧说法，改写正文为新说法，并在其后留一行痕迹：

    > [!outdated] 2026-11-14 更新
    > 原说法"……"（[[旧来源]]）已被 [[新来源]] 取代。

除此之外，新内容一律融进正文相应的段落，不要在页尾追加"补充""更新于某日"小节。页面的历史在操作日志和 git 里。
