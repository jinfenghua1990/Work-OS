# Work OS 原始凭证档案（本地开发版）

## 已实现

- 采购单、咖啡销售 CSV、银行流水 CSV、进项/销项发票登记：**先保存原始文件**，再提交业务记录。
- 通过 `POST /api/archive/files` 流式上传原始字节；生成 SHA-256，存为 `backend/data/archive/<哈希目录>/<哈希>`，同内容文件不覆盖。
- PostgreSQL/SQLite schema 37 包含 `archive_files`、`archive_uploads`、`archive_links` 三张表：分别记录内容、每次上传行为、与业务记录的关联。
- 原件可以在财务中心的「统一原始凭证档案」查看、下载；下载之前检查原件的 SHA-256，发现篡改则拒绝读取。
- 支持 PDF / OFD / XML / CSV / XLS / XLSX / TXT / ZIP 原件的字节保存；**保存文件不等于已解析 Excel 或识别发票内容**。
- 不改变已过账销售、发票、银行流水的会计事实；重传相同原件仅增加上传记录/幂等关联，不重复记账。

## 配置和运行

默认路径：`backend/data/archive`，由 `.gitignore` 排除，不提交到公开 GitHub。

本地/NAS 路径可通过环境变量配置：

```powershell
$env:WORKOS_ARCHIVE_ROOT = "D:\\WorkOS-Archive"
python backend/run_offline.py
```

群晖 NAS 共享文件夹需先在当前电脑/服务端上挂载为可访问的文件路径，API 进程需要有读写权限。不能将数据库连接串或备份密钥写入仓库。PostgreSQL/Neon 只保存文件索引，原始文件仍在 Work OS API 所在设备的档案路径；跨设备使用时必须先规划共享存储和独立备份。

默认单文件上限 50 MiB，可用 `WORKOS_ARCHIVE_MAX_BYTES` 调整；大文件和批量导入还需要后续分块/断点续传方案。1GB/月指**每月合计文件量**，不代表可以一次上传 1GB 文件。

## API 约定

`POST /api/archive/files`：Content-Type: application/octet-stream，原始字节为请求 body，头信息：
- `X-File-Name`: URL 百分号编码的原文件名
- `X-Document-Category`: bank/input_invoice/output_invoice/sales/purchase/expense/contract/other
- `X-Document-Period`: YYYY-MM
- `X-Source-System`: 可选的来源系统名称

返回 `{id, upload_id, sha256, size_bytes, duplicate_content}`。
`GET /api/archive/files`：列出原始凭证及元数据。
`GET /api/archive/files/{id}/download`：校验原件哈希并下载。
`POST /api/archive/links`：`{file_id,source_system,source_document_id}` 建立幂等业务引用。
`GET /api/archive/links?source_system=...&source_document_id=...`：查询业务关联的原件。

业务接口在请求中携带 `archive_file_id` 时，会在同一个数据库事务内关联：
- `POST /api/purchases` → `coffee.purchase / 采购单内部 id`
- `POST /api/sales/import-row` → `coffee.sale / 标准业务来源键`
- `POST /api/bank/import` → `finance.bank_transaction / 流水内部 id`
- `POST /api/invoices` → `finance.invoice / 发票内部 id`

## 尚未完成，禁止作为正式财务归档宣传

1. **身份认证、角色/业务数据授权**：当前 HTTP API 只在 127.0.0.1 上提供，不能公开到互联网或授权其他 Admin 直接使用。
2. **远程对象存储和不可篡改**：文件落地本地/NAS 目录，尚未实现 R2/Neon Object Storage S3 适配，也没有 WORM 锁定与法规要求的保留期审计。文件被系统管理员在磁盘直接删除或修改不受应用层绝对防护。
3. **独立备份与恢复演练**：必须同时备份数据库和原件目录；仅有云数据库备份不代表原始文件已经备份。
4. **原始文件批量解析、Excel/OFD 数据读取、渠道退款及历史导入迁移**：本次只提供档案留存，不能将其标识为所有导入格式均已开发。
5. **凭证合规电子归档**：需另行按适用法规完成真实性、完整性、可用性、保管期限、操作审计及恢复验证。

## 本地测试

```bash
python -m unittest discover -s backend/tests -v
npm ci
npm run build
```

测试运行于临时 SQLite 文件和临时目录，不连接 Neon，也不会写入真实银行流水、发票或采购数据。
