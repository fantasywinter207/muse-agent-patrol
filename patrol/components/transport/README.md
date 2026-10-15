# 经过授权的网络与凭据适配

## Purpose
经过授权的网络与凭据适配。

## Responsibilities
HTTPS REST、有限请求预算、禁止重定向、Credential Manager/私有文件、可选WS握手。

## Non-responsibilities
不保存Key、不解释正文、不自动重试POST。

## Public boundary
`index.py` 导出 Client/Response/BudgetExceeded/AuthError/credential_provider/connect_ready。外部代码不得深导入私有实现。

## Allowed dependencies
config公开配置；Python标准库；可选websocket-client==1.9.2。

## Invariants
401/403停止该tick授权访问；WS连接仅证明连接；凭据不进入日志。

## Testing boundary
通过公开边界使用临时数据库、fake HTTP/WS和真实多进程故障场景核验；不读取真实凭据、不访问业务API。
