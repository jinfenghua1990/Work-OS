# Work OS · 财务具名账号与独立审批（Schema 45）

## 本次代码

- 只有显式设置 `WORKOS_AUTH_MODE=named` 才启用具名账号；默认保留现有**本地单人模式**，不会悄悄接管已有用户。
- 具名角色：`owner`、`finance_clerk`、`finance_approver`、`viewer`。后端验证权限，不能只靠前端隐藏按钮。
- 密码只存 scrypt 哈希；同一设备下以 HttpOnly、SameSite=Strict 的短期会话识别操作人。用户停用后，旧会话下一次请求自动失效。
- 财务录入员可以创建个人垫付待审单；只有有审批权限的**其他具名账户**才能使该单过账。提交、审批人及时间均留在 `finance_personal_advance_reviews`。
- `approved_by` 等审批人标识从服务端会话派生，不能通过接口自行填写来冒名。无明确提交人身份的旧垫付单在具名模式下**拒绝审批**，需人工补齐来源核验。
- 单据摘要仍按原有 `personal_advances` 规则记账，保障一次审批只过账一次；重复重试不复制费用、应付或银行流水。

## 初始化（仅在本地或 NAS 的受控终端操作）

1. 在**测试账套**完成 Schema 44→45 的迁移；迁移 SQL 位于 `backend/migrations/45_named_finance_users.sql`。
2. 在私有配置中显式设置 `WORKOS_AUTH_MODE=named`。联网 Neon 启动仍需 R2/NAS 原有必要配置。
3. 以命令行交互方式创建至少两名不同人员的账号，输入密码时不回显，也不写入命令参数：

```sh
python backend/manage_finance_users.py add --username account01 --role finance_clerk
python backend/manage_finance_users.py add --username reviewer01 --role finance_approver
```

4. 分别使用两名账号在受控本地页面测试“录入→审批→凭证→不可重复入账”，再核验数据库审计记录和月结。

## 安全边界

- 这仍然是**仅绑定本机 loopback 的 API**；未实现面向公网的 HTTPS/反向代理认证、集中持久会话、正式身份提供商接入。不能因此声称已具备生产公网部署条件。
- 已强制角色门禁的高风险接口包括个人垫付审批、银行分配、手工分录、月结、发票税务状态、外币估值与期初余额；**但只有个人垫付有明确的提交人≠审批人约束**，其他高风险操作需后续完善独立审批工单。
- 旧单密码模式不代表正式的具名审批。正式上线必须在配置和备份核验后切换具名模式，防止旧模式绕过角色隔离。
- 未修改正式 Neon 分支，也没有创建默认用户、预置密码或做任何实账过账。
