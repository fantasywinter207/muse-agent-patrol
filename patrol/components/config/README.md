# 配置解析与校验

## Purpose
配置解析与校验。

## Responsibilities
有界预算、来源、作用域和调度默认值。

## Non-responsibilities
不读取API Key、不扩大平台授权。

## Public boundary
`index.py` 导出 Config/load_config。外部代码不得深导入私有实现。

## Allowed dependencies
Python标准库。

## Invariants
未知字段拒绝；写操作默认关闭；URL必须为HTTPS origin。

## Testing boundary
通过公开边界使用临时数据库、fake HTTP/WS和真实多进程故障场景核验；不读取真实凭据、不访问业务API。

