# 🎬 大雄看点映 — 东京华人电影周报

> 面向东京华人社区的微信公众号自动化工具，每周自动抓取 TOHO、United 等主流连锁影院及独立艺术影院的排期，匹配豆瓣评分与短评，自动生成推荐度排行榜，并支持一键审核发布到微信公众号。

---

## ✨ 功能概览

| 模块 | 功能 |
|------|------|
| **Part 1：自动抓取** | 抓取 TOHO、United Cinemas、独立影院的本周 / 下周排期 |
| | 从豆瓣获取中文片名、评分、评价人数、精选短评 |
| | 综合豆瓣评分 + 评价人数 + 放映场次 + 新片加成，自动计算推荐指数 |
| | 输出结构化 Markdown 简报 + JSON 数据文件 |
| **Part 2：审核发布** | Flask 本地 Web 界面，支持在线编辑预览 |
| | 一键将简报发布为微信公众号草稿或直接发布 |

---

## 🗂️ 项目结构

```
daxiongwatchmovies/
├── run_scraper.py          # Part 1：抓取脚本（CLI）
├── app.py                  # Part 2：Flask 审核发布界面
├── config.yaml.example     # 配置文件模板
├── requirements.txt
│
├── scraper/
│   ├── base.py             # 数据类（MovieInfo, ScreeningInfo, TheaterSchedule）
│   ├── toho.py             # TOHOシネマズ 排期抓取
│   ├── united.py           # ユナイテッド・シネマ 排期抓取
│   ├── independent.py      # 独立影院通用抓取（可配置）
│   └── douban.py           # 豆瓣评分 / 短评获取
│
├── generator/
│   └── briefing.py         # Markdown 简报生成 + 推荐度算法
│
├── publisher/
│   └── wechat.py           # 微信公众号 API（草稿 / 发布）
│
├── templates/              # Flask HTML 模板
│   ├── index.html          # 简报列表页
│   ├── review.html         # 审核编辑页（左编辑器 / 右实时预览）
│   └── publish.html        # 发布配置页
│
├── output/                 # 自动生成的简报文件（.md / .json）
└── tests/                  # 单元测试
    ├── test_briefing.py
    └── test_wechat.py
```

---

## 🚀 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置

```bash
cp config.yaml.example config.yaml
# 编辑 config.yaml，填入微信公众号 AppID / AppSecret
# 可自定义独立影院列表（theaters.independent.locations）
```

### 3. Part 1：生成简报

```bash
# 演示模式（不发起真实网络请求，使用内置示例数据）
python run_scraper.py --demo

# 生产模式（抓取真实排期 + 豆瓣评分）
python run_scraper.py

# 跳过豆瓣抓取（速度更快）
python run_scraper.py --no-douban

# 同时输出 JSON 结构化数据
python run_scraper.py --demo --json

# 查看所有选项
python run_scraper.py --help
```

生成的简报保存在 `output/briefing_001_2026-04-13.md`，格式示例：

```
# 大雄看点映 第1期｜4月13日 ~ 4月19日
> 东京华人电影周报

## 📅 本周上映（4月13日 ～ 4月19日）
### ◆. 利益区域（関心領域）
| 项目 | 内容 |
| 🌐 豆瓣 | ★★★★　 8.3 |
| 📍 上映影院 | シアター・イメージフォーラム |
> 💬 用反高潮的方式呈现高潮——这才是最令人不安的恐怖。

## 🏆 本周推荐榜 Top 10
| 🥇 | 利益区域 | 70/100 | 8.3 | ... |
...
```

### 4. Part 2：审核发布

```bash
python app.py
# 打开浏览器访问 http://127.0.0.1:5000
```

界面功能：
- **简报列表**：查看所有已生成的简报文件
- **审核 / 编辑**：左侧 Markdown 编辑器 + 右侧实时预览
- **发布**：选择「仅创建草稿」或「直接发布」，一键推送到微信公众号

---

## ⚙️ 推荐度算法

推荐指数（0-100分）由以下四个维度加权计算：

| 维度 | 权重 | 说明 |
|------|------|------|
| 豆瓣评分 | 50% | 满分10分，线性映射 |
| 豆瓣评价人数 | 20% | log₁₀归一化（1000万人→满分）|
| 本周放映场次 | 20% | 上限20场，越多越高 |
| 本周新上映 | 10% | 新片额外加成 |

权重可在 `config.yaml` 的 `ranking` 节中自定义。

---

## 🏪 影院配置

### 连锁影院
`config.yaml` 中已预配置东京主要 TOHO 和 United Cinemas 影院。可按需增删。

### 独立影院
在 `config.yaml` 的 `theaters.independent.locations` 中添加：

```yaml
- name: "新影院名称"
  url: "https://theater-website.jp"
  schedule_path: "/schedule/"
```

抓取器会自动适配常见的日本影院网页结构。

---

## 🧪 运行测试

```bash
python -m pytest tests/ -v
```

---

## 📋 常见问题

**Q: 豆瓣抓取很慢或被封？**  
A: 增大 `config.yaml` 中的 `douban.request_delay`（默认 1.5 秒）。

**Q: 微信发布报错 40001？**  
A: AppID / AppSecret 填写有误，或公众号未认证。

**Q: 独立影院没有抓取到数据？**  
A: 该影院可能使用了 JavaScript 动态渲染。可手动将排期粘贴到 `output/briefing_xxx.md` 后再审核发布。
