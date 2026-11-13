# 对照前的收录（简版）

对照任务里的收录只求把材料登记进知识库，不展开整理：

1. 完整读完 `raw/…/source.md`。
2. 写来源摘要页 `wiki/sources/<材料标题>.md`：开头链接原文 `[[raw/…/source|原文]]`，然后是概要、核心观点（逐条）、局限与存疑。frontmatter 填 `type: source`、`description`、`raw`、`doc_date`、`topics`、`concepts`、`mentions`、`updated`。
3. 材料里的概念：`_context/candidates.md` 里已有对应页面的，把新内容融进那一页并加上出处；没有页面的一律记进来源页的 `mentions`，本次不新建概念页。
4. 起名、链接、出处、分歧的写法见 `_skills/vault-conventions/SKILL.md`。
