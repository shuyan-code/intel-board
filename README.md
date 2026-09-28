# Intel Board 本地情报看板

把消息、链接、情报和优惠统一入库，保留来源和核验状态，提供作者统计、分类检索及本地网页看板。使用 Python 标准库和 SQLite，无需安装依赖。

## 启动

```powershell
cd intel-board
python app.py
```

打开 http://127.0.0.1:8770 。首次启动自动建库并载入明确标注为演示的样例。`INTEL_DB` 可指定数据库路径，`INTEL_PORT` 可指定端口。

## 导入真实资料

网页右上角可上传 CSV 或 JSON。字段：`type`（message/link/intel/deal）、`title`、`summary`、`url`、`platform`、`author`、`published_at`、`category`、`confidence`、`verified`、`tags`。JSON 可为对象数组。相同来源 URL 或相同标题与平台会去重。也可在“公开来源”页面添加 RSS/Atom 地址或 GitHub 仓库（例如 `https://github.com/cline/cline`），点击采集。公共来源只抓取可公开访问的数据；Discord、QQ、Telegram 等私有记录需自行合法导出后导入。

## 功能与边界

- 消息、链接、情报、优惠统一存储；按关键词、类型、平台、分类及核验状态筛选。
- 作者贡献计数、平台统计、近期情报和优惠栏目。
- 每条记录保存原始链接、采集时间和核验状态，避免把社区说法误作事实。
- GitHub 仓库抓取公开 issue/PR；RSS/Atom 抓取公开订阅内容。公开 API 有访问频率限制。
- 没有截图中的数据库及 22 个社区账号授权，无法真实复原其“4 万条历史数据”或私人群消息；演示数据仅供展示。

## 测试

```powershell
python -m unittest discover -s tests -v
```

## 安全

服务默认仅监听 127.0.0.1。公开部署前应增加登录、上传限制及采集任务隔离。请勿把个人聊天记录、密钥或数据库文件推送到公开仓库。
