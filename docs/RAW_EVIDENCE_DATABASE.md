# Work OS 原始凭证 R2 存储（V42）

## 已确定架构

| 存储位置 | 内容 | 用途 |
|---|---|---|
| Neon PostgreSQL public | 业务单、银行流水、发票、归档索引、SHA-256、上传批次 | 唯一业务数据源 |
| Neon PostgreSQL evidence | 原始行快照、来源字段、不可变 R2 定位信息 | 可核验追溯，不依赖业务解析结果 |
| Cloudflare R2（私有 Bucket） | Excel、PDF、CSV、XML、OFD、ZIP 等完整原文件字节 | 新上传原件的云端主存储 |
| 本地/NAS | 每份原始文件的独立副本 | 备份及灾难恢复 |

V41 已有的 BYTEA 原件不删除、不覆盖，仍可读取。V42 新文件在 \`evidence.original_documents\` 保留 SHA-256、大小和时间等元数据，\`exact_bytes\` 为 NULL，实际字节写入私有 R2 Bucket；\`evidence.r2_locations\` 保留不可覆盖的定位信息。原始行、上传批次仍保留在原有 \`evidence.row_snapshots\`、\`evidence.import_batches\`，避免破坏原来的业务与追溯引用。

**不使用 Neon 对象存储。** 这里的 R2 指 **Cloudflare R2**，不是 Neon Storage。

## 安全逻辑

- R2 路径固定为 \`work-os/originals/前2位/后2位/sha256\`，不使用用户文件名作为对象路径；
- 写入使用 S3 API \`IfNoneMatch="*"\`，同内容文件重传不覆盖既有对象；不同原件生成新 SHA；
- 上传后实际 GET 回原文件字节，验证完整 SHA-256 和大小，再允许数据库事务提交；
- 下载通过后端鉴权读取 R2，并再次校验 SHA-256；不公开 R2 对象 URL；
- R2 无法访问、哈希异常、未配置密钥时，上传失败，不会伪造“原件已保存”或入账成功；
- NAS 副本在本地归档根目录保存，通过哈希验证；**NAS 必须单独备份、设置权限**；
- \`evidence.r2_locations\` 由数据库 UPDATE/DELETE 触发器保护。管理员仍有可能绕过，不能当作满足法规要求的 WORM 保管证明。

## 配置

仅在本机或部署环境的 **私有环境变量** 中设置（不要提交密钥到 GitHub）：

\`\`\`env
WORKOS_DB_BACKEND=postgres
WORKOS_DATABASE_URL=postgresql://...
WORKOS_EVIDENCE_STORAGE=r2
WORKOS_R2_ACCOUNT_ID=your_32_hex_cloudflare_account_id
WORKOS_R2_BUCKET=work-os-originals
WORKOS_R2_ACCESS_KEY_ID=your_r2_access_key_id
WORKOS_R2_SECRET_ACCESS_KEY=your_r2_secret_access_key
WORKOS_ARCHIVE_ROOT=/path/to/nas-or-local-archive
\`\`\`

需在 Cloudflare 手工创建 **私有 R2 Bucket** 并为其发放最小权限凭证。数据库连接字符串和 R2 密钥不要发给聊天、写入仓库或提交日志。

安装依赖：\`python -m pip install -r backend/requirements.txt\`。

PostgreSQL 默认采用 R2 模式（未配置凭据时拒绝上传）；本地 SQLite 默认保留原 BYTEA/BLOB 归档模式供离线测试。可通过 \`WORKOS_EVIDENCE_STORAGE=database\` 显式使用 PostgreSQL V41 兼容模式，但这会继续占用 Neon 数据库存储，不推荐长期使用。

## 版本

- Neon 测试库和正式库之前已初始化 V41。
- 本改造目标版本 **V42**；需在隔离副本与回归测试通过后再执行实际迁移。
- 新增 R2 定位表，旧 SQL 原件 \`exact_bytes\` 变为可空；**旧原件保持不变**。
- 不复制旧库业务金额、不修改会计凭证，不制造不存在的历史原文件。

## CoffeeBear 历史凭证导入

旧 CoffeeBear Neon 数据库只有部分文件索引，真实文件在旧 Mac / NAS 路径；仅靠旧库中的银行交易、发票金额无法重建原始 Excel、PDF。

\`backend/import_coffeebear_originals.py\` 默认 **只读检查**。需在能访问旧路径的 Mac / NAS 本机运行：

\`\`\`bash
COFFEEBEAR_DATABASE_URL='postgresql://read-only-user:...' \
python backend/import_coffeebear_originals.py \
  --map-path '/Users/gino/ecommerce-workspace-data/production=/Volumes/NAS/production' \
  --map-path '/data=/Volumes/NAS/data'
\`\`\`

只有全部源文件可读取、实际大小及 SHA-256 校验通过，且 Work OS 正式库及 R2 配好之后，才可显式执行：

\`\`\`bash
WORKOS_COFFEEBEAR_IMPORT_ALLOW=1 \
python backend/import_coffeebear_originals.py --apply \
  --map-path '/Users/gino/ecommerce-workspace-data/production=/Volumes/NAS/production' \
  --map-path '/data=/Volumes/NAS/data'
\`\`\`

命令中的 NAS 路径仅为示例，必须换成**实际挂载位置**。每个旧上传记录通过 \`archive_links\` 保留源类型与旧 ID，重复执行时跳过已关联记录。导入只归档原件，**不自动创建任何会计分录或改写业务账**。若未找到历史原件，脚本拒绝部分导入并输出缺失项。

另有 \`backend/backfill_evidence_db.py\` 用于处理此前已有 Work OS \`archive_files\` 元数据的本地原件，V42 的 R2 模式下会优先补录 R2。

## 费用与限制

R2 对象容量、请求量、网络费用以用户 Cloudflare 账单及当期官方定价为准。已有 Neon 免费数据库容量保护（V41 BYTEA 默认 256 MiB）仅适用于兼容的 SQL 字节存储，不再限制 V42 的 R2 新原件。不过数据库原始行快照及索引仍占用 Neon 存储。

本次代码改造**不能代表真实 R2 已连通**：没有 Cloudflare Bucket 和私有访问密钥就无法验证真实上传。CoffeeBear 历史原件的实际复制也依赖 Mac/NAS 上存在相应原始文件。

## 上线验收

1. 本地与 CI 测试通过，隔离 Neon 结构从 V41 升至 V42，核对旧文件可读；
2. 在用户的私有 Bucket 中，用**真实**测试文件验证上传 / 回读 / 重复上传 / 删除保护；
3. 核对 NAS 独立镜像、R2 访问权限和恢复方法；
4. 用 CoffeeBear 真实文件运行只读清点，再迁移，核对总数量、上传批次及 SHA-256；
5. 验证业务使用中的登录鉴权与财务入账逻辑，方可宣布上线完成。
