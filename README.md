# 🎬 大熊看点映 — 东京华人映画周报

本项目以 eiga.com 为数据源（Step1）抓取东京上映电影，并在 Step2 将电影与豆瓣匹配以获取中文名、评分与短评，最终生成 Markdown 简报供人工审核与发布。

核心设计（精要）

- Step1（pipeline/step1_eiga.py）
  - 仅使用 eiga.com 抓取：登记东京影院（theaters）并抓取正在上映的电影（movies、screenings）。
  - 分类与快照：master movies 表在首次遇见电影时写入主记录；每次抓排行会向 movie_snapshots 写入当天的排名快照（snapshot_date, rank, movie_id），Step1 输出基于该快照生成 MD。小众/院线的判定规则见下文。
  - 记录 run_state.last_run_date（以日本时区 JST），当天已成功抓取则跳过当日重复抓取。
  - 数据库存于 data/eiga.db（SQLite），包含 tables: theaters, movies, screenings, movie_snapshots, run_state。

- Step2（pipeline/step2_douban.py）
  - 在 Douban 上匹配电影并抓取评分/短评，结果写入 douban_matches，并保存历史快照到 douban_matches_history。
  - 支持断点/恢复：默认 resume 模式会跳过已匹配并 verified 的条目；可通过参数只重跑未匹配条目。
  - 支持跳过名单（douban_skip_list）：把找不到豆瓣的演唱会等加入跳过名单，后续 Step2 自动跳过。

可用脚本/接口（快速参考）

- Step1
  - python run_step1.py
    - 登记并抓取最新上映电影（会检查 run_state.last_run_date，若当天已抓取则跳过）。
  - 强制重跑（清除 last_run_date）:
    - sqlite3 data/eiga.db "DELETE FROM run_state WHERE key='last_run_date';"
  - 产出文件说明：
    - 输出的 Markdown: output/step1_YYYY-MM-DD.md
    - 额外会写入 output/step1_run_state.json，包含 last_run_jst（日本时区的 ISO 时间）和生成的 MD 路径，便于快速确认 Step1 的最后成功执行时间。

- Step2
  - python step2_api.py run [--no-md] [--delay N]
    - 已匹配（记下了 douban_id，无论自动还是手动）的条目静默跳过，不产生 log、不发请求；一旦匹配上就永不重搜、永不删除（verified 仅作年份/国名校验的参考信息）。
    - 搜索失败过的电影自动加入 douban_skip_list，后续 run 静默跳过，等待手动匹配。
    - --no-md：不生成 MD 报告文件。
  - 产出文件说明：
    - 输出的 Markdown: output/step2_YYYY-MM-DD.md
    - Step2 生成的 MD 在末尾会追加”未匹配”表格，列出所有没有 douban_id 的电影（便于人工核对）。

CSV 脚本（scripts/douban_csv.py）：

# 导出
python scripts/douban_csv.py export
python scripts/douban_csv.py export output/my_export.csv

# 导入（修改 CSV 里的 douban_url 后）
python scripts/douban_csv.py import douban_export.csv
导出列：movie_id / category / rank / title_jp / title_original / year / country / douban_id / title_cn / douban_url / douban_score / douban_votes / verified / search_attempts / in_skip_list / skip_reason

导入规则：
douban_url 有值 → upsert 进 douban_matches（verified=1, search_attempts=0），同时从 skip list 移除
douban_url 为空 → 删除该电影的 match 记录，下次 step2 会重新搜索



- Step2 手动匹配
  - python step2_api.py manual-set <movie_id> <douban_id> [--delay N]
    - 当自动搜索找不到结果时，在豆瓣网站手动找到对应页面，将 URL 中的数字 ID 填入。
    - 示例：豆瓣页面 https://movie.douban.com/subject/36608656/ 则 douban_id 为 36608656。
    - 执行后会拉取该豆瓣页面，解析评分/导演/演员/年份/国家/短评等，写入 douban_matches（verified=1）和 douban_details。
    - 手动匹配的电影在后续 step2 run 中会被静默跳过（不会覆盖）。
    - 示例：
      - python step2_api.py manual-set eiga_12345 36608656

- 未登录电影 ID 更新（remap_unlisted_id）
  - 当 Step1 在影院排片页面发现 eiga.com 未收录的电影时，会自动分配 99999xxx 临时 ID 并写入 DB。
  - 一旦该电影被 eiga.com 正式收录（获得真实 movie_id），可用以下方式将临时 ID 替换为正式 ID：
    ```python
    from pipeline.step1_eiga import remap_unlisted_id
    result = remap_unlisted_id("999990001", "93590")
    ```
  - 执行效果：
    - 从 eiga.com 抓取 93590 的真实元数据（国家、年份、导演、海报等）补填原记录。
    - 将 screenings / showtimes / douban_matches / douban_details / eiga_reviews 等 8 张子表的 movie_id 级联更新为新 ID。
    - 删除旧的 99999xxx 行，eiga_url 更新为正式地址。
  - 保护机制：旧 ID 必须是 99999 开头；新 ID 不能已存在于 DB，否则抛 ValueError。

- Step2 跳过名单管理
  - python step2_api.py skip add <movie_id> [--reason <reason>]
    - 将 movie_id 加入 douban_skip_list，Step2 以后静默跳过（适用于演唱会、活动等非电影条目）。
  - python step2_api.py skip remove <movie_id>
    - 从跳过名单中移除，恢复自动匹配。
  - python step2_api.py skip list
    - 列出所有跳过条目。

数据库表要点

- run_state: 用于记录 last_run_date（JST ISO 日期）。
- douban_matches: movie_id -> douban_id / score / votes / verified / manual / matched_at / search_attempts。
  - 只要记下了 douban_id（自动或手动）即视为已匹配，Step2 不会再搜索、也不会删除该记录。
  - verified=1/0 仅表示年份+国名校验是否通过（参考信息），不再触发删除或重搜。
  - manual=1 表示人工用 manual_set_match 确认的匹配；这类记录受额外保护，连「未匹配清理」也豁免。
  - search_attempts 记录自动搜索失败次数，达到 3 次后 Step2 静默跳过；--rerun-unmatched 可重置。
- douban_matches_history: 每次抓取的评分快照。
- douban_details: 手动匹配或 enrich_top_movies 后写入的完整页面数据（导演/演员/短评/预告片等）。
- douban_skip_list: 永久跳过名单（reason, noted_at），仅影响 Step2 自动搜索，不影响 Step3。

影院分类规则

- 新的判定原则（已应用于 pipeline/step1_eiga.py）：
  - 若某电影在东京上映的任一电影院属于“院线影院”（theaters 表中 chain 字段非空），则该电影被归为「院线 (chain)」。
  - 若该电影在东京有上映影院，但所有上映影院都不属于院线（chain 字段为空），则归为「小众 (indie)」。
  - 若在东京找不到任何上映影院，则归为「其他 (other)」。

- 说明与影响：
  - 该规则不再依赖地理区域关键字（以前基于地区关键词判断的小众逻辑已废弃）。
  - Step1 在生成 MD 时会展示该电影的所有已知上映影院（不再受地区限制），避免出现“电影没有写上映影院”的遗漏情况。
  - movie_snapshots 保存的是按排名的快照：snapshot_date, rank, movie_id（movie_id 对应 master movies 表的主键）。Step1 的 MD 输出基于该快照和 master 表的元数据（片名/原题/年份/国家）生成。


工作流建议

1. 每天运行 run_step1.py（或由调度器触发）。Step1 会在当天第一次成功运行后记录 last_run_date，避免重复抓取同日数据。
2. 运行 step2_api.py 在 douban 上补全/刷新评分。建议默认不频繁强制重抓已有评分的条目。
3. 对于明确不是电影（演唱会、活动）或在豆瓣上找不到条目的记录，使用 add_movie_to_skip_list 标注为跳过。

运维与安全注意

- 请勿将含敏感 Cookie 的文件（如 _douban_cookies.json）保存在代码仓库中。推荐使用 Playwright 的持久化 profile（.playwright_profile）来保存登录态，或在本地 config 中设置 cookie 字符串（谨慎）。
- 若需手动操作 DB，请在脚本停止状态下使用 sqlite3 CLI 执行修改，避免并发写入冲突。

项目特定说明（来自 .github/copilot-instructions.md）

- 请不要使用空白或临时 profile。
- 由于 VS 终端粘贴时可能会逐字符导致长命令被破坏，请优先将复杂命令写入脚本（.py 或 .ps1），再通过短命令执行，例如：
  - python _script.py
  - powershell ./_script.ps1
