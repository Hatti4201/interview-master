# Interview Master Database

本地 PostgreSQL 是面试资料的母版。核心 Schema 只使用标准 PostgreSQL，可恢复到 AWS RDS、Supabase 或其他 PostgreSQL 平台。

## 启动和导入

```bash
docker compose up -d
python3 scripts/import_master.py
```

默认连接：

```text
postgresql://interview_master:local_interview_only@127.0.0.1:54329/interview_master
```

该密码只用于绑定在 `127.0.0.1` 的本地数据库，不可用于云端。

## 验证

```bash
python3 scripts/verify_master.py
```

## 本地题库前端和 API

启动 PostgreSQL、后端 API 和独立前端：

```bash
docker compose up -d
python3 backend/server.py
# 另开一个终端
python3 -m http.server 8010 --bind 127.0.0.1 --directory frontend
```

打开 `http://127.0.0.1:8010`。API 入口是 `http://127.0.0.1:8011/api`，支持题目搜索、公司/Vendor/分类交叉筛选、问题详情和单场面试详情。

默认“面试记录”视图按日期、公司、Vendor、轮次、类型、岗位、面试官和候选人逐列筛选；展开任意一行可查看该场全部问题。“按问题查看”保留去重问题和频次视图。

手机宽度下，面试记录自动改为卡片视图；组合筛选和展开问题仍保留，不需要横向拖动表格。

## 用户和权限

- `Admin`：读取题库、添加面试、创建用户、修改角色和停用用户。
- `User`：只读题库。
- 密码使用随机盐和 `scrypt` 哈希；Session 和 CSRF token 存在 PostgreSQL。
- 创建或重置管理员时用隐藏密码输入，不把明文密码放进命令或源码：

```bash
python3 backend/server.py --create-admin admin@kanidata.com
```

登录与权限端到端自检：

```bash
python3 backend/smoke_test.py
```

## 部署配置

前端是 `frontend/` 静态文件，后端是 `backend/server.py`，数据库是标准 PostgreSQL。推荐的 AWS 对应关系：

1. `frontend/` → 私有 S3 Bucket，CloudFront 作为唯一公开入口。
2. 后端容器 → Lambda Function URL；Lambda 加入私有子网并使用 `interview-master-api-sg` 连接 RDS。
3. PostgreSQL 17 → RDS for PostgreSQL；开启自动备份。
4. CloudFront 默认路径 `*` 指向 S3，`api/*` 指向 Lambda Function URL，并关闭 API 缓存、转发 Cookie/查询参数/`Origin`/`X-CSRF-Token`。

让前端和 API 共用一个 CloudFront 域名，可以继续使用 `SameSite=Lax` Session Cookie，也不需要把数据库暴露到公网。生产容器由根目录 `Dockerfile` 构建；不要直接把本地开发服务器暴露到互联网。

后端环境变量：

```text
INTERVIEW_MASTER_DATABASE_URL=postgresql://...
INTERVIEW_FRONTEND_ORIGIN=https://library.example.com
INTERVIEW_COOKIE_SECURE=true
INTERVIEW_COOKIE_SAMESITE=Lax
```

前端会在本地使用 `api-base`，部署到域名后自动改用同域 `/api/*`，不需要手工修改文件。本方案要求 CloudFront 配置 `api/*` Origin；若以后改为跨站点 API，则需要显式配置后端 HTTPS 地址并使用 `SameSite=None`（同时必须启用 Secure）。

## 构建并推送 Lambda API 镜像

本机验证镜像：

```bash
docker build -t interview-master-api:local .
```

Lambda 镜像包含 AWS Lambda Web Adapter，现有 HTTP 服务不需要改写。Apple Silicon Mac 直接构建 `linux/arm64`；以下示例使用 Oregon 区域：

```bash
export AWS_REGION='us-west-2'
export AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
export ECR_REPOSITORY='interview-master-api'
export ECR_URI="$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/$ECR_REPOSITORY"

aws ecr create-repository \
  --region "$AWS_REGION" \
  --repository-name "$ECR_REPOSITORY" \
  --image-scanning-configuration scanOnPush=true

aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"

docker buildx build \
  --platform linux/arm64 \
  --provenance=false \
  --tag "$ECR_URI:2026-08-15" \
  --push .
```

如果 ECR 仓库已经存在，跳过 `create-repository`。镜像白名单只包含 `backend/` 和 `database/`；`raw/`、`backups/`、前端和 Session 均不进入镜像。

## Lambda 控制台设置

1. 创建 `Container image` Lambda，选择 ECR 中的 `interview-master-api:2026-08-15`，架构选 `arm64`，内存从 512 MB 开始，Timeout 设为 30 秒。
2. VPC 选择 `interview-master-vpc`、两个私有子网和 `interview-master-api-sg`；RDS Security Group 只允许该组访问 TCP 5432。
3. 设置 `INTERVIEW_MASTER_DATABASE_URL`、`INTERVIEW_FRONTEND_ORIGIN=https://你的CloudFront域名`、`INTERVIEW_COOKIE_SECURE=true`、`INTERVIEW_COOKIE_SAMESITE=Lax`。
4. 创建 Function URL，Auth type 选择 `NONE`；应用自身仍要求邮箱、密码、Session 和角色权限。

## 上传前端并配置 CloudFront

创建私有 S3 Bucket 并上传静态前端：

```bash
export FRONTEND_BUCKET="interview-atlas-$AWS_ACCOUNT_ID"

aws s3api create-bucket \
  --region "$AWS_REGION" \
  --bucket "$FRONTEND_BUCKET" \
  --create-bucket-configuration LocationConstraint="$AWS_REGION"

aws s3api put-public-access-block \
  --bucket "$FRONTEND_BUCKET" \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

aws s3 sync frontend/ "s3://$FRONTEND_BUCKET/"
```

CloudFront 控制台设置：

1. 默认 Origin 使用该 S3 Bucket 和 Origin Access Control，不使用 S3 Website endpoint。
2. 添加 Lambda Function URL HTTPS Origin，并增加优先级更高的 `api/*` Behavior。
3. `api/*` 使用 `CachingDisabled`，允许全部 HTTP Methods，Origin request policy 使用 `AllViewerExceptHostHeader`。
4. 默认 Behavior 只需 `GET/HEAD`，Default root object 设置为 `index.html`，Viewer protocol 设为 Redirect HTTP to HTTPS。
5. CloudFront 域名生成后，更新 Lambda 的 `INTERVIEW_FRONTEND_ORIGIN`；以后更新前端用 `aws s3 sync` 后创建 CloudFront invalidation。

## 首次迁移到 RDS

先创建空的 RDS PostgreSQL 17 数据库 `interview_master`，再从本机执行：

```bash
export RDS_DATABASE_URL='postgresql://数据库用户:密码@RDS_ENDPOINT:5432/interview_master?sslmode=require'
pg_restore --verbose --no-owner --no-acl \
  --dbname="$RDS_DATABASE_URL" \
  backups/interview-master-2026-08-01-auth.dump
```

恢复完成后核对数量，确认一致后再让 Lambda 连接 RDS：

```bash
INTERVIEW_MASTER_DATABASE_URL="$RDS_DATABASE_URL" python3 scripts/verify_master.py
```

## 创建可移植备份

```bash
mkdir -p backups
pg_dump --format=custom --no-owner --no-acl \
  --exclude-table-data=app_sessions \
  --dbname='postgresql://interview_master:local_interview_only@127.0.0.1:54329/interview_master' \
  --file=backups/interview-master.dump
```

恢复到其他 PostgreSQL：

```bash
pg_restore --no-owner --no-acl --dbname="$DATABASE_URL" backups/interview-master.dump
```

当前可恢复母版是 `backups/interview-master-2026-08-01-auth.dump`，SHA-256 是 `10f075c3dc63067ef631cd7eba7cbc463b158e321b346692d803c7f72410ad16`。它包含用户账户和密码哈希，不包含登录 Session；该文件必须放在私有存储，不能进入公开 S3 Bucket 或 Git。

## 数据规则

- `raw/` 中的源文件不修改。
- 英文写入 `questions.canonical_text_en`，中英文原文保存在 `question_variants.original_text`。
- 暂时不能可靠翻译的中文/中英混合问题使用英文占位标识，并设置 `needs_review = true`。
- 两个半结构化 TXT 已按详细记录块解析；低置信度记录保留行号并标记 `needs_review`。
- 低置信度的回答、代码输出和说明文字只保留在原文中，不自动进入标准问题表。
- 中文和中英混合问题已完成英文翻译与复合问题拆分；`needs_review` 当前为 0。
- 拆分决策保存在 `question_split_review.json`，子问题继续关联原面试和完整原文。
- 明文密码不进入数据库、备份或 Git；数据库只保存带随机盐的密码哈希。
- Session 行不写入可移植备份，恢复后所有用户重新登录。
