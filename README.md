# yyds邮箱域名监测

独立跑的域名库存和 yyds 补位监控，仓库：`domainslot`。作者：`biubiubiu125`。版本：`v0.0.1`。

它只做这几件事：

- 用多把阿里云 AccessKey 拉域名列表，并在补位时按域名所属账号写解析
- 用多个 yyds 账密登录 JWT，监测自定义域名增减和实时泛解析配额
- 有泛解析空位、又有「未使用」域名时，马上加进 yyds 并按 `dns-guide` 写 TXT / MX / 通配 MX
- 人在 yyds 官网删掉域名后，本地保持「已使用」，再从未使用库存补另一条

它不做：OpenAI 注册、接码、删除 yyds 域名、改 gptimage2api、同步 API Key 域名范围。

镜像：`ghcr.io/biubiubiu125/domainslot:v0.0.1`

## 本地启动

```powershell
Set-Location -LiteralPath 'C:\Users\Administrator\codex-1\domainslot'
Copy-Item .env.example .env
```

把 `.env` 里的 `PANEL_PASSWORD` 和 `SECRET_KEY` 改成自己的，然后：

```powershell
docker compose up -d --build
```

面板默认只在本机：`http://127.0.0.1:8788`

首次接入已有阿里云 / yyds 账号时只对账，不改已有解析；当时拉到的阿里云域名全部记为「已使用」。之后新出现的域名才是「未使用」。

## 环境变量

| 变量 | 说明 |
| --- | --- |
| `PANEL_PASSWORD` | 面板登录密码 |
| `SECRET_KEY` | 加密库里 AccessKey / yyds 密码 / cookie 的主密钥，至少 16 位 |
| `DATABASE_URL` | PostgreSQL 连接串 |
| `YYDS_API_BASE` | 默认 `https://maliapi.215.im/v1` |
| `YYDS_POLL_SECONDS` | yyds 轮询间隔，默认 20 |
| `ALIYUN_POLL_SECONDS` | 阿里云轮询间隔，默认 60 |

## 更新镜像

仓库每次 push 都会跑 GitHub Actions：先测，再推 GHCR。

```powershell
Set-Location -LiteralPath '<deploy-dir>'
docker compose pull
docker compose up -d
docker compose ps
```
