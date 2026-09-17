# yyds邮箱域名监测

独立跑的域名库存和 yyds 补位监控。仓库名 `domainslot`。作者 `biubiubiu125`。当前版本 `v0.0.1`。

**推荐用 Docker Compose 跑。** 本机可以在仓库目录构建；服务器只拉 GHCR 镜像，不要 `git clone`，也不要在服务器上 `pip install`。

- 仓库：https://github.com/biubiubiu125/domainslot
- 镜像：`ghcr.io/biubiubiu125/domainslot:v0.0.1`（默认分支还会打 `latest`）
- 面板：`http://127.0.0.1:8788`
- 数据库：PostgreSQL（Compose 一并拉起，不要外接别的库）
- 时区：`Asia/Shanghai`

已确认需求在 [docs/yyds邮箱域名监测-总方案.md](docs/yyds邮箱域名监测-总方案.md)。

## 做什么

- 用多把阿里云 AccessKey 拉域名列表；写解析时打到**拥有该域名的那把钥匙**
- 用多个 yyds 账密登录 JWT，监测自定义域名增减和实时泛解析配额
- 有泛解析空位、又有「未使用」域名时，马上加进 yyds，并按这次返回的 `dns-guide` 写 TXT、根 MX、通配 MX
- 人在 **yyds 官网**删掉域名后，本地这条保持「已使用」，再从未使用库存补**另一条**

NS 仍留阿里云，不把域名 NS 改到 yyds。

## 不做什么

- 不接入 gptimage2api，不管注册机、接码、API Key、OpenAI 是否拉黑
- **不能删除 yyds 域名**：面板没有删除按钮，也不会调用 yyds 删除接口
- 第一次对账只监视，**不改已有解析**
- 未使用排队期间不提前写 TXT / MX
- 不用 yyds 的 `AC-` API Key 管域名；加域、验证、开泛解析只用登录 JWT

拉黑由人看注册情况，自己去 yyds 官网删。本监控只负责发现空位或删减后补位。

## 怎么工作

```
轮询 yyds（约 20 秒）→ 轮询阿里云（约 60 秒，多账号错开）→ 最多补 50 次（删减优先）
```

1. **首次接入**某个阿里云账号：当时拉到的域名一律「已使用」，不写解析，不往 yyds 加。
2. **对账之后**新出现的域名才是「未使用」，按阿里云注册时间从早到晚补。
3. 某个开着「接收新域名」的 yyds 号还有泛解析空位，并且库存里有未使用域名：立刻加域并写解析。
4. 扫到某个 yyds 号少了域名：被删的那条继续「已使用」，优先给**那个号**补另一条（该号仍须开着接收，且泛解析还有空）。
5. 未实名、赎回、ClientHold、NS 不是阿里云：标异常，不硬改。
6. 列表对不上配额、配额未知：占用记成 `-1`，这一轮拒绝对账、拒绝加域。

满员以**泛解析名额**为准，不是自定义域名总数。VIP 即使还能加域名，泛解析用完也视为这个号满，去填下一个开着接收的号。

启动时会自动给 PostgreSQL 补缺的列，不用单独跑迁移。补位锁用 `filling_at` 和 PostgreSQL advisory lock，没有独立 jobs 表。健康检查打 `/api/healthz`（库通并且工作线程活着）；详细 `/api/health` 需要登录。

不要对 app 做多副本：会抢同一把补位锁、重复加域。

## 面板怎么用

打开 `http://127.0.0.1:8788`，用 `.env` 里的 `PANEL_PASSWORD` 登录。面板不要直接挂公网，本机或 SSH 隧道访问即可。

1. 先加**阿里云账号**（AccessKey ID + Secret，建议 RAM 子账号）。权限至少要能拉域名列表、查注册状态（实名 / ClientHold / NS）、查/加/删解析。
2. 等第一次对账结束。已有域名会变成「已使用」。未实名或赎回的会进「异常」。
3. 再加 **yyds 账号**（用户名 + 密码）。号如果开了 2FA，把一次性码填进「一次性二验码」，登录成功后会清掉，不会反复用。
4. 打开该号「接收新域名」，需要的话改「补位顺序」。多个号同时有空位时，填满顺序靠前的再填下一个。关掉接收的号仍监测，但不会自动往里加。
5. 域名被 OpenAI 拉黑后：去 **yyds 官网**删除。这里只会看到少了一条，然后从未使用里补另一条。
6. 想指定下一条就只把那条留成「未使用」。想让一条已删域名再被用，人手动改成「未使用」。
7. 「从本监控移除」只删本系统里的账号记录，不会动 yyds 网站上的域名。

第一屏能看出未使用 / 已使用 / 异常数量、各 yyds 号满不满、登录是否失败、阿里云是否限流、最近补位和失败原因。点「立即扫描」会提前跑一轮。

## 自动构建 GHCR

可以。代码进 GitHub 之后会自动测、自动构建、自动推镜像。本地 `git commit` 不会打镜像，必须 `git push`。

工作流是 `.github/workflows/docker-publish.yml`：

- 触发：任意分支 `push`，以及手动 `workflow_dispatch`
- 先跑 `compileall` 和 `pytest`；测试不过就不会推镜像
- 通过后用 `GITHUB_TOKEN` 登录 GHCR（权限已声明 `packages: write`）
- 推送到 `ghcr.io/biubiubiu125/domainslot`，平台 `linux/amd64` + `linux/arm64`
- 每次都会打 `VERSION` 文件里的版本（当前 `v0.0.1`）和 `sha-……`
- 默认分支额外打 `latest`
- 其它分支还会打分支名标签

服务器更新等 Actions 绿了再：

```bash
cd /opt/domainslot
docker compose pull
docker compose up -d
```

当前 GHCR 包默认不能匿名拉。服务器第一次 pull 前先登录，Token 至少要有 `read:packages`：

```bash
echo '这里换成你的 GitHub Token' | docker login ghcr.io -u biubiubiu125 --password-stdin
```

## 环境变量

复制 `.env.example` 为 `.env` 再改。`SECRET_KEY` 至少 16 位，用来加密库里的 AccessKey、yyds 密码和 cookie。**改过之后旧密文解不开，只能在面板里把账号密钥重填一遍。** 不要把 `.env` 提交到 Git。

| 变量 | 说明 |
| --- | --- |
| `PANEL_PASSWORD` | 面板登录密码。第一次启动写入库；之后改 `.env` 再用新密码登录一次，会更新库里的哈希 |
| `PANEL_COOKIE_SECURE` | 登录 Cookie 是否只走 HTTPS。本机 `http://127.0.0.1` 保持 `false` |
| `SECRET_KEY` | 加密主密钥，至少 16 位，部署后不要换 |
| `DATABASE_URL` | PostgreSQL 连接串。本机仓库 compose 连内部服务 `db`；服务器 compose 连 `postgres` |
| `YYDS_API_BASE` | 默认 `https://maliapi.215.im/v1` |
| `YYDS_POLL_SECONDS` | yyds 轮询间隔，默认 20 |
| `ALIYUN_POLL_SECONDS` | 阿里云轮询间隔，默认 60（多账号会再错开几秒） |
| `VERIFY_ATTEMPTS` | 加域后验证次数，默认 12 |
| `VERIFY_RETRY_SECONDS` | 验证重试间隔秒，默认 15 |
| `TZ` | 默认 `Asia/Shanghai` |
| `LISTEN_HOST` / `LISTEN_PORT` | 容器内监听，默认 `0.0.0.0:8788`。对外是否只绑本机看 compose 的 `ports` |
| `DOMAINSLOT_IMAGE` | 服务器 compose 用的镜像，默认 `ghcr.io/biubiubiu125/domainslot:v0.0.1` |

## 本机 Docker Compose

在仓库目录。compose 会起应用和 PostgreSQL。数据库服务名是 `db`，用户名 / 库名 / 密码都是 `domainslot`，只适合本机，服务器不要沿用这个密码。

改代码、看未发布改动，用本地构建：

```powershell
Set-Location -LiteralPath 'C:\Users\Administrator\codex-1\domainslot'
Copy-Item .env.example .env
```

把 `.env` 里的 `PANEL_PASSWORD` 和 `SECRET_KEY` 改成自己的，然后：

```powershell
docker compose up -d --build
docker compose ps
docker compose logs --tail=80 app
```

只想跑已经发布的镜像：

```powershell
Set-Location -LiteralPath 'C:\Users\Administrator\codex-1\domainslot'
docker compose pull
docker compose up -d
```

面板：`http://127.0.0.1:8788`

停掉（不带 `-v` 不会删库，数据在卷 `domainslot-pg`）：

```powershell
Set-Location -LiteralPath 'C:\Users\Administrator\codex-1\domainslot'
docker compose down
```

本机备份：

```powershell
Set-Location -LiteralPath 'C:\Users\Administrator\codex-1\domainslot'
docker compose exec -T db pg_dump -U domainslot domainslot > domainslot.sql
```

## 服务器 Docker Compose

root 用户，Docker 和 Compose 已装好。目录用 `/opt/domainslot`，只拉 GHCR，不写 Nginx / SSL。PostgreSQL 不映射到宿主机。

| 项 | 值 |
| --- | --- |
| 目录 | `/opt/domainslot` |
| 镜像 | `ghcr.io/biubiubiu125/domainslot:v0.0.1` |
| 数据库 | `postgres:16-alpine`，用户名 / 库名 `domainslot` |
| 监听 | `127.0.0.1:8788` |
| 容器 | `domainslot-app`、`domainslot-postgres` |
| 网络 / 卷 | `domainslot-net`、`domainslot-postgres-data` |

### 1. 部署前检查

下面整段一起跑。某一项不对就先停，不要覆盖旧目录。

```bash
whoami
. /etc/os-release && echo "$PRETTY_NAME"
docker --version
docker compose version
ss -lntp | grep ':8788' || echo '8788 空闲'
ls -ld /opt /opt/domainslot 2>/dev/null || true
docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' | grep -E 'NAMES|domainslot' || true
docker network ls --format '{{.Name}}' | grep domainslot || echo '无同名网络'
docker volume ls --format '{{.Name}}' | grep domainslot || echo '无同名数据卷'
df -h /opt /var/lib/docker
openssl version
```

正常时应是 root、8788 空闲、`/opt/domainslot` 还不存在、没有旧的 domainslot 容器 / 网络 / 数据卷，磁盘建议至少留 5GB。

### 2. 建目录

```bash
mkdir -p /opt/domainslot
cd /opt/domainslot
pwd
```

### 3. 写 `.env`

只在首次部署时生成。已经有 `.env` 就不要重跑，否则解开不了旧库里的密钥，也连不上现有数据卷。

```bash
cd /opt/domainslot

PANEL_PASSWORD="$(openssl rand -hex 16)"
SECRET_KEY="$(openssl rand -hex 32)"
POSTGRES_PASSWORD="$(openssl rand -hex 32)"

cat > .env <<ENV_FILE_END
PANEL_PASSWORD=${PANEL_PASSWORD}
PANEL_COOKIE_SECURE=false
SECRET_KEY=${SECRET_KEY}
POSTGRES_USER=domainslot
POSTGRES_DB=domainslot
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
DATABASE_URL=postgresql+psycopg://domainslot:${POSTGRES_PASSWORD}@postgres:5432/domainslot
YYDS_API_BASE=https://maliapi.215.im/v1
YYDS_POLL_SECONDS=20
ALIYUN_POLL_SECONDS=60
VERIFY_ATTEMPTS=12
VERIFY_RETRY_SECONDS=15
TZ=Asia/Shanghai
LISTEN_HOST=0.0.0.0
LISTEN_PORT=8788
DOMAINSLOT_IMAGE=ghcr.io/biubiubiu125/domainslot:v0.0.1
ENV_FILE_END

chmod 600 .env
grep '^PANEL_PASSWORD=' .env
sed -E 's/^(PANEL_PASSWORD|SECRET_KEY|POSTGRES_PASSWORD|DATABASE_URL)=.*/\1=******/' .env
```

结束标记 `ENV_FILE_END` 必须单独占一行。正常结束后提示符从 `>` 回到 `#`。`grep` 那行是面板密码，立刻抄下来。`SECRET_KEY` 不是登录密码，部署后不要换。

### 4. 写 `docker-compose.yml`

这一段单独完整执行。结束标记 `COMPOSE_FILE_END` 必须单独占一行，前面不能有空格。如果执行完还停在 `>`，按 Ctrl+C，从本节开头重来。

```bash
cd /opt/domainslot

cat > docker-compose.yml <<'COMPOSE_FILE_END'
services:
  postgres:
    image: postgres:16-alpine
    container_name: domainslot-postgres
    restart: unless-stopped
    environment:
      POSTGRES_USER: domainslot
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: domainslot
      TZ: ${TZ:-Asia/Shanghai}
    volumes:
      - domainslot-postgres-data:/var/lib/postgresql/data
    networks:
      - domainslot-net
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U domainslot -d domainslot"]
      interval: 5s
      timeout: 5s
      retries: 10

  app:
    image: ${DOMAINSLOT_IMAGE:-ghcr.io/biubiubiu125/domainslot:v0.0.1}
    container_name: domainslot-app
    restart: unless-stopped
    depends_on:
      postgres:
        condition: service_healthy
    env_file:
      - .env
    environment:
      DATABASE_URL: postgresql+psycopg://domainslot:${POSTGRES_PASSWORD}@postgres:5432/domainslot
      TZ: ${TZ:-Asia/Shanghai}
    ports:
      - "127.0.0.1:8788:8788"
    networks:
      - domainslot-net
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8788/api/healthz', timeout=4)"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 25s

volumes:
  domainslot-postgres-data:
    name: domainslot-postgres-data

networks:
  domainslot-net:
    name: domainslot-net
COMPOSE_FILE_END

tail -n 8 docker-compose.yml
docker compose config --quiet && echo "Compose 配置正确"
docker compose config | grep -E 'image:|published:|host_ip:|container_name:|name: domainslot'
```

结尾应有 `name: domainslot-net`。应用端口应是 `127.0.0.1:8788`，PostgreSQL 不应出现 `ports`。

### 5. 拉镜像并启动

```bash
cd /opt/domainslot
docker compose pull
docker compose up -d
docker compose ps
docker compose logs --tail=100 app postgres
```

将拉取 `ghcr.io/biubiubiu125/domainslot:v0.0.1` 和 `postgres:16-alpine`。

pull 若报 401 / denied / unauthorized，先按上面「自动构建 GHCR」登录再拉。连不上 `ghcr.io` 时不要改镜像名。

正常情况：`domainslot-postgres` 已 healthy；`domainslot-app` 稍后也会 healthy；端口是 `127.0.0.1:8788->8788/tcp`；日志里有 `yyds邮箱域名监测 v0.0.1 已启动`。看持续日志用 `docker compose logs -f app`，Ctrl+C 只退出日志，不停容器。

### 6. 本机确认

```bash
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8788/
curl -sS http://127.0.0.1:8788/api/healthz
ss -lntp | grep ':8788'
docker port domainslot-postgres
docker inspect domainslot-app --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}'
docker inspect domainslot-postgres --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}'
```

首页应 `200`。`/api/healthz` 应是 JSON 且 `"ok": true`（刚启动大约 25 秒内可能 503，等 app 变成 healthy 再查）。监听应是 `127.0.0.1:8788`，不是 `0.0.0.0`。`docker port domainslot-postgres` 不应有输出。两个容器网络都应是 `domainslot-net`。

然后打开 `http://127.0.0.1:8788`，用刚才抄下的 `PANEL_PASSWORD` 登录，按「面板怎么用」加账号。先阿里云、等首次对账，再加 yyds。

## 更新镜像

会短暂重启应用容器。数据卷和 `.env` 都保留，**不要重新生成 `.env`**。

先确认 GitHub Actions 里这次 push 已经构建成功，再：

```bash
cd /opt/domainslot
docker compose pull
docker compose up -d
docker compose ps
curl -sS http://127.0.0.1:8788/api/healthz
```

当前 compose 钉的是 `v0.0.1`。要跟 `latest` 时，改 `.env` 里的 `DOMAINSLOT_IMAGE` 后再 pull。

## 备份和恢复

服务器备份（同时把 `/opt/domainslot/.env` 拷走，没有 `SECRET_KEY` 解不开库里的密钥）：

```bash
cd /opt/domainslot
docker compose exec -T postgres pg_dump -U domainslot domainslot > domainslot-$(date +%F).sql
```

恢复会覆盖当前库：

```bash
cd /opt/domainslot
docker compose stop app
docker compose exec -T postgres psql -U domainslot -d domainslot < domainslot-YYYY-MM-DD.sql
docker compose start app
```

把 `YYYY-MM-DD` 换成实际文件名。

## 停止和卸载

只停容器、保留数据和配置：

```bash
cd /opt/domainslot
docker compose down
```

连数据卷一起删（域名库存、账号、日志都会没）：

```bash
cd /opt/domainslot
docker compose down -v
```

## 常见问题

**面板起不来 / 容器反复重启**

看 `docker compose logs app`。没设 `PANEL_PASSWORD`，或 `SECRET_KEY` 短于 16 位，进程会直接退出。

**`/api/healthz` 返回 503**

工作线程还没起来，或已经挂了。刚启动大约 25 秒内健康检查可能还是 starting。

**镜像 pull 401**

GHCR 当前不能匿名拉。用 `biubiubiu125` 登录，Token 要有 `read:packages`。

**push 了但服务器 pull 不到新镜像**

本地提交不会打镜像。看 GitHub Actions「Verify and Publish」是否绿了；测试失败不会推。默认分支的 `latest` 和 `v0.0.1` 都要等这次 workflow 跑完。

**登录提示次数过多**

同一 IP 短时间密码打错会被 429。等一分钟再试。

**yyds 账号标红、不再加域**

登录失败（改密、开了验证码 / 2FA、限流）。改密码或填一次性二验码后保存，点「立即扫描」。一个号失败不影响其它号。

**阿里云限流**

该号会退避，其它阿里云账号继续。解析永远打到域名所属账号，不会串号。

**首次接入后旧域名被拿去加 yyds**

不该发生。先看该阿里云账号的「首次对账」时间，对账没完成不要改状态。

**官网删了域名，又被加回去**

不该发生。被删的那条会保持「已使用」。只有你手动改成「未使用」才会再拿它补。

**配额显示未知、不再补位**

yyds 列表和套餐对不上，或配额没读到 used。占用会记成 `-1`，这一轮不加域。看操作日志里的 `yyds_list_unreliable`。

**域名一直异常**

常见原因：未实名、ClientHold、NS 不是阿里云、409（可能仍绑在别人账号或官网删除还没完成）、解析验证一直没过。看该行「说明」和操作日志，不要在本监控里删 yyds 域名。
