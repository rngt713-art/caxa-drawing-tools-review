# 实现说明（图纸上传助手修复）

日期：2026-11-05。实现模型：DeepSeek V4.1 Flash。验收方：Codex。
工作目录：`本地评审快照`；`app/` 为可修改副本，原始代码未改。

## 1. 范围与约束

- 保持 Tk + Python 编排 + C# helper 子进程架构；不引入云/服务/复杂安全体系。
- 本机具备 CAXA 2011r3 引用 DLL；修订后的 helper 已用 x86 `csc` 编译通过。全部业务
  测试仍为离线 unittest / 桩对象，**未连接业务服务器、未上传图纸、未发起真实流程**。
- 不改工作流 `user_id` 身份绑定策略；不取消“高层失败→低层兼容回退”的冗余。
- ZJ03.* 规则保持现状（拒绝多字母前缀），需业务确认，文档不擅自放宽。
- 只修改本工作区；未改 Codex 的外部验收脚本。

## 2. 主要修改

### 2.1 文件内容绑定（TASK 1）

- 新增 `app/staging.py`：每个任务独占暂存目录，把**用户选中的源文件**复制为规范名，
  复制后校验 SHA-256/大小；先写临时文件并校验，再检查同名冲突（Windows 大小写不敏感），
  最后原子提交。冲突/校验失败不会覆盖已暂存字节，更不会碰源文件。
- `app/caxa/uploader.py::upload_item` 只上传暂存副本；编辑图号/名称后按
  `expected_drawing_filename()` 生成目标名，界面显示实际目标（`filename_warnings()`），
  源文件不改名、不被替换。
- 上传前再次核对指纹；缺失/冲突/不符都显式报错，不假成功。

### 2.2 关系唯一来源与业务规则（TASK 2、11）

- 新增 `app/relations.py`：`assemblies`、`manual_borrows`、`item.parent_code` 在导入时
  **一次性迁移**为 canonical `relations`，之后不再合并；GUI 删除的边不会复活。
- 默认规则：设计树中不存在→`own`（Reference=False），已存在→`borrow`（Reference=True）；
  `manual_override` 永远优先；不再强制 03.* 全部 borrow，文件名前缀不参与判定。
- 分类决策在**任何写操作之前**写入 SQLite 日志（kind=`decision`）；同一任务重试/重启
  恢复该决策，不会因为上次已创建 Part 就把 own 改成 borrow；新任务第一次一定现场查询
  设计树，不信任上传包里的 `auto_decided`。
- 分类完成后再校验有效图：检测环、own 多父；两父现有子件自动 borrow 合法，两父缺失
  子件才报 own 多父（不再在分类前误报）。
- borrow 只读查找后链接：父/子缺失分别记“缺父/缺子”，**绝不新建** Parts（含整机根零件）。

### 2.3 关系失败显式统计、阻止自动发流程（TASK 3）

- 每条关系都有逐条结果（已借用/已归属/已存在/冲突/缺父/缺子/未执行/待核实/失败）。
- 已有链接类型不符→“冲突”，类型无法确认→“待核实”，查询失败（`children_error` 且无
  可用 endpoint 证据）直接报错且**不写**；不会把 `bool('false')` 当成 True。
- 文件失败、关系失败、关系待核实、必需关系未执行（`borrow_missing=false`）、没有已验证
  Drawing、流程未确认启动，任一存在即 `ok=False` 且不自动发流程；UI 汇总文件/关系/流程结果。

### 2.4 轻量持久化与防重（TASK 4）

- 新增 `app/journal.py`（标准库 SQLite）：
  - `tasks`：任务、服务器/端口/登录账号、摘要、状态、阶段；
  - `operations`：阶段、状态、远端对象 ID、ClassID/类名、版次、link_type、错误与证据。
- `OP_RUNNING` 视为“待核实”（进程崩溃/断电留下的执行中记录不会被自动重放）；任务级
  `run_lock` 独占锁覆盖“查找/创建任务 + 远端操作”，防止双开并发写。
- 任务身份 = 服务器 + 账号 + PDF 内容（含 SHA-256/大小），**不含**关系与流程选项：
  - 仅改关系、仅补发流程不会重传 PDF；
  - 内容或账号/服务器变化才形成新任务；
  - 相同批次再次点击返回 duplicate 结果，不再创建版次/流程；
  - “新建任务”是显式操作，但**不能绕过待核实**。
- 待核实恢复：`app/upload_recover.py`（`list` / `confirm` / `no-write` / `release-lock`）
  与 UI“核实待定结果”按钮；人工只更新指定操作（成功需给 ID/类型/版次证据；确认未写入
  才允许重试），其它已确认成功保留，不需要删库或整批重传。
- 不做跨机器 exactly-once 的承诺：本地日志是恢复辅助；同批图纸建议一人提交。

### 2.5 bridge 与 helper（TASK 5）

- `app/caxa/bridge.py`：进程句柄与登录状态同锁管理；**任何新进程先登录**再收业务命令；
  登录返回 false/`logged_in=false` 判失败；超时/丢响应标记 `timed_out`/`response_lost`；
  非幂等写（upload/create_part/link_part/reference/start_workflow/...）不自动重试。
- `CaxaError` 结构化保留 `stage/detail/created_process_instance_id/partial/uncertain/result`，
  `as_dict()` 不含凭据。
- `caxa_helper.cs`：
  - 登录检查返回值与 `HasLogin`，换账号先 Logout；
  - 流程 create 成功后所有失败路径带 `created_process_instance_id` 与 `stage`；
  - 上传返回**实际** ClassID（append 成功路径改用 `existingDuplicate.ClassID`，修掉空引用）；
  - `CmdUpload` 外层 try/finally 维护“上传命令执行中”，未预期异常返回
    `partial/uncertain`（附已知 object_id），不再当普通失败重放；
  - `new_version` 覆盖文件失败时放弃检入并返回结构化错误，不再假成功；
  - 自动确认：删除按进程名/标题的广泛白名单；只在 `upload` 命令执行期间、且窗口属于
    **本 helper 进程**时处理；先唯一定位“追加新的版次”，读 `BM_GETCHECK` 确认选中后才点
    唯一“确定”，多处命中/无法确认则一律不点。

### 2.6 UI 冻结、快照与并发（TASK 9）

- `set_busy()` 统一禁用所有操作按钮、账号/服务器/策略/流程控件与清单选择；所有编辑、
  拖拽、勾选、增删关系、预检、手工借用、加载上传包入口都有 busy 拦截。
- 工作线程只读 `upload_ui_logic.snapshot_settings()` / `snapshot_manifest()` 生成的冻结
  快照，不再读 Tk 变量；日志/状态/结果统一经事件队列回 UI 线程。
- 完成/失败/异常都恢复交互；若上传期间账号/服务器被改动，结果不再合并成功状态；条目
  修改（图号/名称/类型/成品代码/父级）会失效原成功状态并重传。
- UI 编辑原子化：先校验全部字段再写；改图号同步迁移入/出边并同步 `item.parent_code`，
  删除父级组会删除其下所有边；刷新不再从旧集合恢复关系。

### 2.7 文档类型、版次与流程（TASK 7、10）

- 上传返回实际 ClassID：2D 图纸要求 1002 Drawing，工艺文档要求 1003 CappDocument；
  回退到其他类型→“待核实”，不进入流程集合、不自动删除。
- `append_iteration` 的**所有**成功路径都回读版次/历史并比较，无增长→待核实（保留
  object_id）；`reuse` 明确表示复用旧文档；`part_drawings` 查询失败视为“未知”而非“无”。
- 流程要求 `started=true` 且 process_id>0；否则“待核实”并保留已有流程 ID。工作流
  启动后必须回读 `WFProcessInst.state=2` 和完整 Drawing 集合；回读失败同样记为
  “待核实”。工作流 Drawing 集合由日志中全部已确认上传重建（旧成功 + 新成功）。
- 整机 Part 已建但挂接失败→部分完成/待核实（带 created_part_id），不记正常成功；
  `Login` 返回 false 不会报 ok。

### 2.8 上传包校验（TASK 8）

- `load_bundle` / `validate_bundle`：schema、版本（未知拒绝）、字段类型、kind 枚举、
  必填 machine、重复图号、路径穿越、文件存在性、导入时 SHA-256/大小核对；手工 PDF
  首次登记指纹，提交前复核。
- 仅对**选中执行**的行做严格字段校验：未勾选的异常行可保留供人工修改，不阻塞有效批次。

## 3. 测试与结果

离线（无 CAXA/无网络/无 EXE）：

| 套件 | 命令 | 结果 |
| --- | --- | --- |
| 本工作区回归 | `python -m unittest discover -s tests -v` | **54 tests OK** |
| 独立验收 | `python tests\run_external_acceptance.py ..\independent_acceptance.py` | **19 tests OK** |
| 边界验收 | `python tests\run_external_acceptance.py ..\boundary_acceptance.py` | **9 tests OK** |
| 真实 Tk UI | `python tests\run_external_acceptance.py ..\ui_acceptance.py` | **4 tests OK** |

说明：外部脚本要求把临时目录建在其自身目录旁（本工作区外）。本机受限令牌无法在那些
位置创建 0o700 目录，因此 `tests/run_external_acceptance.py` **只读加载**外部脚本，仅把
`tempfile.TemporaryDirectory` 重定向到 `tests/_tmp`（默认权限），并为本机缺失的
`tkinterdnd2` 提供 `tests/_stub_deps` 桩；未修改外部脚本。Codex 侧可直接运行原脚本。

覆盖点包括：8 项旧复现的正确预期、own/borrow 默认与人工优先、借用缺对象不建、重试语义
稳定、删除关系不复活、重复提交与重启恢复、部分流程错误 ID、超时/丢响应待核实、关系失败
不发流程、文件哈希/版本/路径校验、UI 忙时冻结、待核实恢复命令。

## 4. 构建与分发

- `build_all.ps1`（工作区根）：`-All` / `-Helper` / `-Uploader` / `-Recognizer`；
  `-PlanOnly` 只检查环境与依赖；上传端、识别端分别 PyInstaller 打包到
  `dist\uploader`、`dist\recognizer`。
- `app\caxa\caxa_helper\build_helper.ps1`：需 CAXA 2011 引用 DLL；找不到会明确失败并
  拒绝在无源码对应关系时覆盖旧 exe（`-Force` 才覆盖且自动备份）；成功打印 exe SHA-256。
- `pack_source.ps1`：生成源码交付 zip + `SHA256SUMS.txt`，默认排除
  `caxa_helper.exe`、`__pycache__`、`config.json`、`dist/build`、测试临时目录。
- 仓库已删除旧的 `app\caxa\caxa_helper\caxa_helper.exe`（旧二进制，避免冒充新版）。
  发布前必须：工作设备编译 helper → 小批现场验收 → 记录 exe/源码 SHA-256。

## 5. 未实现 / 现场待验（明确限制）

1. helper 已编译；真实 ClassID、版次回读、关系 endpoint 字段、自动确认窗口归属仍需在
   业务客户端现场验收。
2. 权限/身份策略未改：`user_id` 仍由调用方传入；未新增权限拒绝分类，高层→低层回退冗余
   按用户要求保留（底层是否真正鉴权只能由受限账号现场验证）。
3. 不做跨机器 exactly-once；本地日志 + 独占锁只覆盖单机并发与重启恢复。
4. ZJ03.* 规则未放宽，需业务确认；工艺/图纸 ClassID 白名单以现场实测为准。
5. 自动确认只在 helper 自己的进程窗口生效；若实际 SDK 对话框属于其他进程，将**不点击**
   并留给人工选择（宁可不点，不误点）。
6. 旧 `new_version` 路径仅做“复制失败不检入/不假成功”的保守修复；默认 UI 仍为
   `append_iteration`/`reuse`。
7. 本机存在少量早期实验/旧版测试临时目录（0o700 ACL 无法删除），打包脚本已排除，
   不影响源码交付与测试（`tests/` 下正常测试用例不使用它们）。

## 6. 现场最小验收表

| 场景 | 预期 |
| --- | --- |
| 正常账号上传图纸/工艺文档/追加版次 | 类型分别为 Drawing/CappDocument，版次实际增长 |
| 已发布/他人检出 | 明确失败或待核实，不靠 fallback 改写 |
| 新建 own 子件挂到所选父级 | Reference=False，父级不更换 |
| 借用缺失零件 | 不新建任何 Part，逐条报缺父/缺子 |
| 关系类型冲突 | 报冲突，不改写，不自动发流程 |
| 流程 create 后中断 | 保留流程 ID，状态待核实，可用 `upload_recover.py` 处理 |
| 同批重复点击 / 重启 | 不重复版次/流程；改关系或补发流程不重传 PDF |
| 上传时手工打开其他 CAXA 对话框 | 不会被自动点击 |
