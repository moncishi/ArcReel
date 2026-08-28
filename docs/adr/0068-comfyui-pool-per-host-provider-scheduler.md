---
status: accepted
---

# 每台 ComfyUI 主机一个自定义供应商，执行层池调度分配任务

ComfyUI 是自托管的节点式生成服务，MiniMax H3 视频工作流需要在自己的 GPU 上跑。多台
ComfyUI 主机组成的池，配置与分配语义必须回答两个问题：**池怎么配**（在哪声明成员、容量
是多少）与**任务怎么分**（何时、由谁、选哪台主机）。

我们决定：**每台 ComfyUI 主机 = 一个 `comfyui-video` 自定义供应商**（各有独立 base_url
与 `video_max_workers`），池成员在 worker 认领期由 `discover_comfyui_pool` 聚合发现；
容量分两层——**claim 层**按池总量（各主机 `video_max_workers` 之和）限流，**执行层**由
池调度器 `ComfyUIPoolScheduler`（`lib/custom_provider/comfyui_scheduler.py`）经健康检查
（GET /queue 读 running/pending）选**最空闲**主机、按节点租约强制「每主机 ≤2 在途」。

## 两层容量：池总量限流 + 单主机租约

- **claim 层（pool 总量）**：`CapacityTable` 对虚拟池 id `comfyui-pool` 的容量 = 各成员
  `video_max_workers` 之和（N 台各 2 → 池容量 2N）。池满任务保持 queued；worker 认领时
  把池内任一主机解析到的 provider 投影到 `comfyui-pool`，按总量判定「满」。
- **执行层（单主机 ≤2）**：`execute_video_task` / `execute_reference_video_task` 在执行
  前经 `select_pool_host` 获取调度器租约（`acquire_comfyui_node`）。租约语义 = 节点级
  互斥的「上传 + 提交」原子窗口：同一节点同时只有一个租约持有者，且 `running + pending +
  reserved < 2`（COMFYUI_MAX_TASKS_PER_NODE）才放行。租约持有超过 30 分钟自动过期释放
  （`COMFYUI_MAX_RESERVATION_MS`），防止进程内残留锁死整台主机。

两层叠加得到「池内任意主机不超载、池总并发 = 各主机之和」。任务被 claim 但调度器在等
主机空位时，ArcReel 状态是 running（任务占着 ArcReel 槽、在等上游容量）——与既有
provider 排队语义一致，可接受。

## 选中主机 pin 进执行 payload，而非改 resolver

`select_pool_host` 返回 `(host, lease)` 后，执行入口把选中主机写成
`video_provider_<cap> = <provider>/<model>` 复合值 pin 进执行 payload
（`pin_pool_host_payload`）。`resolve_video_backend` 对 payload 恒取最高优先级，因此
重解析会命中选中主机：**checkpoint 冻结的主机身份、backend 构造、续跑绑定三者一致**。
不改 resolver 分层逻辑，非池 provider 零影响。

租约生命周期：`select_pool_host` 获取 → 经 `resolve_generation_context` 构造 backend →
`ComfyUIVideoBackend` 提交成功后在 `on_provider_submitted` 回调释放（幂等）→ 外层
`try/finally` 兜底再释放一次（幂等），覆盖 resolve/checkpoint/生成异常与 reuse 早退路径，
保证每个出口恰释放一次、不占满调度器 30min 过期窗口。

## 明确不采用

- **单内置 provider + 主机 URL 列表**：让一个 ComfyUI provider 内部持有多台主机。pool
  语义会落在 provider 解析层之外，容量、健康检查、租约全要重做，且「provider 是一个
  base_url」的既有契约（价格、连接测试、模型目录）都被破坏。把「一台主机」等价于
  「一个自定义供应商」复用了全部既有配置面。
- **claim 层直接按单台主机限流**：认领期无法预知哪台主机空闲，按单台容量限流要么
  over-admit（各台并行空闲时总量超发）要么 under-admit（单台满载但池有闲主机）。
- **跨主机 failover 重试**：提交瞬间主机挂掉（连接错误 / 5xx）不自动转移到其他主机。
  跨主机重试需包住整段 `generate_video_async`（checkpoint 写入、计费 bracket、版本提交），
  整段重试有重复计费 / 重复 checkpoint 风险，违反 ADR 0007「不得对已提交请求二次扣费」。
  参考实现 omnishift 同样没有跨主机 failover——提交失败即 release 租约让任务失败 / 等待，
  由 worker 重试机制处理。`select_pool_host` 的 `exclude_base_urls` 参数已为将来接入
  预留，但执行入口未接线。
- **resume 重新选主机**：resume 走 checkpoint 冻结的 `video_provider_<cap>`（具体主机）
  重解析，不触发池选择——prompt 只在创建它的主机上可查，跨主机轮询会 404。

## Consequences

- 多台主机的池语义由「配置多个 comfyui-video 自定义供应商」天然表达；新增/下线主机 =
  新增/停用供应商，无独立的「池」配置面。
- 池任务的 worker claim 投影是虚拟 `comfyui-pool`，执行解析是具体 `custom-N`，两者必然
  不等——`DispatchProviderChanged` 对池任务豁免，避免死循环回队。
- 主机满载时任务保持 queued（claim 层池满过滤）或 running 等调度器（claim 后），都不会
  向 ComfyUI 无限提交。
- 单主机探测用池内第一个 api_key（各主机 key 不一致时只影响可达性判定，提交仍用各主机
  自身 key）。
- **未来升级路径**：单 provider 多端点（一个 base_url 列表的 ComfyUI 供应商）需要新的
  调度层整合（provider 内健康检查、端点选择、pin 语义进 provider 而非执行入口），并重估
  跨主机 failover 的安全重试边界。落地时须另立 ADR，本 ADR 的「一主机一供应商」边界即
  被取代。
