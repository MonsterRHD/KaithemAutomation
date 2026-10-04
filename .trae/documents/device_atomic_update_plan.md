# 设备更新原子化（改名/跨模块迁移）实施计划

## 背景 / Repository Research

现场运维在设备编辑页（`/device/<name>/manage`，表单 POST 到
`/devices/updateDevice/<devname>`）保存设备时，可同时：

- 修改配置 JSON（含改名）；
- 改存到另一个模块 / 资源（`temp.kaithem.store_in_module/resource`）。

核心实现：[devices.py](file:///Users/ding/Documents/swe/0910031/project-02/kaithem/src/devices.py) 的 `updateDevice()`（L994-1201）；
Web 入口：[devices_interface.py](file:///Users/ding/Documents/swe/0910031/project-02/kaithem/src/devices_interface.py) L179-188。

### 现有流程（问题所在）

`updateDevice()` 在一个 `modulesLock` 临界区里按顺序做：

1. 校验目标模块/资源（模块存在、同名资源必须是 device 类型、`module_lock`、资源 `lock`、`ensure_module_path_ok`）；
2. 取当前运行时对象、旧模块/资源、旧配置目录；
3. **先 `close_device(devname)` 并立即 post `/devices/removed/`**（L1091-1093）；
4. `json.loads(kwargs["json"])`（L1100）——JSON 无效时设备已关闭、removed 事件已发出，但模块资源、缓存、磁盘仍是旧名字；
5. `shutil.copytree` 复制配置目录后 `rmtree` 旧目录（L1121-1142）——复制半途失败会留下半成品新目录，且设备已关闭；
6. `makeDevice()` 重建（L1149）——真实插件类初始化抛错时会被吞掉降级为 `UnsupportedDevice`（L1292-1294），旧设备无法恢复；
7. `rawDeleteResource` 删旧资源，再 `storeDeviceInModule` 写新资源（L1190-1197）——两步之间写盘失败 = 模块里设备资源丢失 / 新旧并存；
8. 最后 post `/devices/added/`。

### 关键事实（已核实）

- 设备名是全局命名空间。子设备全名 = `父名/子名`，由父插件在 `__init__` 中
  `create_subdevice()` 创建（iot_devices 0.34.0 `device.py` L329-417）；
  `Device.close()` 默认先关闭全部子设备（L954-980）。
- 子设备配置是模块中**独立的 device 资源**（约定放在 `父资源所在目录/父名.d/子资源`，
  见 `DeviceRuntimeState.__init__` L328-339；也允许自定义资源路径，如测试里的 `devtest_subdevice`）。
- `device_data_cache[全名] = (module, resource, config)`（L123）：
  `DeviceResourceType.on_load` 写入；设备创建时 `get_config_for_device()` **pop 消费**（L907）。
  现有 `updateDevice` **完全不处理子设备**：父改名后，缓存/资源里仍是 `旧父名/子名`，
  新父插件按 `新父名/子名` 查不到配置 → 子设备配置丢失、旧条目变幽灵、父子关系断裂。
- 标签名 `/devices/<全名>.<datapoint>` 为全局弱引用表（tagpoints `allTags`），
  设备 close + gc 后随对象回收；告警挂在标签上，close 时 release/close
  （`on_after_device_removed` L439-479）。父/子改名后标签、告警名全部随之改变，
  只能靠"整棵树关闭后按新名重建"迁移。
- 资源写盘：`raw_insert_resource` 先改内存 `ActiveModules` 再 `save_resource`
  直接写 yaml（[modules_state.py](file:///Users/ding/Documents/swe/0910031/project-02/kaithem/src/modules_state.py) L396-434、L257-307），
  无回滚；`rawDeleteResource` 先 pop 内存再删文件（L445-464）。
  二者均不触发 `on_load/on_update`（只有 `modules.createResource/handleResourceChange` 才触发），
  因此资源搬迁必须继续只用 raw 原语，自行维护缓存与运行时。
- `deferred_loaders` 只在启动 `finished_reading_resources=False` 时入队（L186-190），
  运行期不再追加；运行期不会有延迟加载器用旧名字复活设备。重启时只从磁盘
  `ActiveModules` 收敛：磁盘上必须恰好保留一个版本。
- 同名冲突 / 设备类型检查的现有口径在 `on_load` 的 `load_closure`（L154-177）：
  同名设备属于不同 (module, resource) → "shares name"；旧设备类型不属于
  `unsupported/unknown/unusedsubdevice` 时，新旧 `type` 必须一致。
- 测试约束：pytest 在 Linux Docker 中运行（`/dev/shm/kaithem_tests`，conftest 限制写盘），
  本机为 macOS，无法直接跑全套环境；验证以新增 pytest + 静态检查（py_compile/ruff）为主。

## 目标

设备更新分两阶段：**先校验和准备（旧设备全程在线），再一次切换整棵设备树**——
父设备、全部子设备、配置目录、模块资源、`device_data_cache`、标签、告警在同一事务里切换；
任一阶段失败则回滚：继续使用原设备，恢复原目录与原资源，不留幽灵标签 / 重复设备 /
断裂父子关系。中止后重启从磁盘收敛到旧版本（唯一版本）。事件仅反映最终可见设备集合。
保留现有模块锁、资源锁、同名冲突、设备类型检查语义。

## Files and Modules

- `kaithem/src/devices.py`（主要改动，外科手术式重构 `updateDevice`，新增少量模块内辅助函数）：
  - 重写 `updateDevice()` 为"准备 → 提交 → 失败回滚"；
  - `makeDevice()` 增加一个仅内部使用的严格参数（插件类初始化失败时抛错而非降级）；
  - 不改动 `DeviceResourceType`、`delete_bookkeep`、`storeDeviceInModule` 的对外行为。
- `kaithem/src/tests/test_devices.py`（新增原子性测试，不改现有测试断言）。

不改动：modules_state.py、devices_interface.py、前端模板、iot_devices 库。

## 事务设计

### 阶段 A：校验与准备（不触碰任何现有状态；旧设备继续运行）

全部在 `modules_state.modulesLock`（RLock，与现状一致）内：

A1. 解析并弹出 `temp.kaithem.store_in_module/resource`；**最先**执行
    `json.loads(kwargs["json"])`，过滤 `temp.`/`filedata.` 键，得到 `savable_data`，
    校验/补齐 `name == 新名`。JSON 无效直接报错，状态零改动。

A2. 保留现有目标侧检查：目标模块存在；目标资源不存在或为 device 类型；
    模块 `module_lock`；目标资源 `lock`；`ensure_module_path_ok(m, r)`。

A3. 旧设备必须在运行时中（父设备）。读取旧 (module, resource) 与旧资源 dict。

A4. **设备类型检查**（与 `load_closure` 同口径）：旧运行时类型不属于
    `unsupported/unknown/UnusedSubDevice` 时，新配置 `type` 必须与旧类型相同，否则报错。

A5. **枚举整棵设备树**（旧全名 → 新全名）：
    - 运行时：`devices_host.get_devices()` 中名 == 旧名或以 `旧名 + "/"` 开头者；
    - 仅配置未实例化的子设备占位：`device_data_cache` 中同前缀键；
    - 取并集，逐项记录旧 (module, resource, config)。运行时动态创建、无持久化资源的
      子设备（如 DemoDevice 的 `requested_*`）不参与资源/目录迁移（与重启行为一致）。
    - 新全名 = 新父名 + 旧全名去掉旧父名前缀后的部分；子配置 dict 深拷贝并改写 `name`，
      保留 `is_subdevice` 等全部其它键。

A6. **子资源新位置映射**：
    - 默认约定路径：`dirname(新父资源 or "") + "/" + ".d/".join(新全名.split("/"))`
      （复刻 `DeviceRuntimeState` L328-339 的规则）；旧默认路径同法由旧父资源算出。
    - 旧资源位于旧默认前缀下 → 映射到新默认前缀下对应相对路径；
    - 旧资源为自定义路径（如 `devtest_subdevice`）：父设备**留在同模块**时保持原资源路径
      （只改配置里的 name 与缓存键）；**跨模块迁移**时搬到新模块的新默认前缀下
      （basename 冲突则在本阶段直接报同名冲突）。
    - 父资源新位置沿用现有规则：`r or 新名`。

A7. **同名冲突预检**（覆盖父与每个子设备的新全名）：
    - 新全名已在运行时且不在本次旧树集合中 → 报错；
    - 新全名在 `device_data_cache` 中且其 (module, resource) 不等于计划位置 → 报错；
    - 每个计划 (新module, 新resource) 若已被占用，其存储的 device 全名必须是本次被迁移的
      旧全名，否则报错；对每个新资源路径做 `ensure_module_path_ok`。

A8. **配置目录预拷贝（暂存）**：对每一对新旧
    `__filedata__/<resource>.config.d`：
    - 新旧相同：跳过；
    - 否则把旧目录 `copytree` 到**新位置旁边的临时暂存目录**
      （`.kaithem.staging.<uuid>` 同级，保证同文件系统），旧目录原样保留；
    - 复制失败 → 清理暂存目录并报错（旧设备在线，未受影响）。
    - 保留现有的路径深度防御检查（rmtree 目标必须 `/` 段数 > 3）。

A9. **写盘预检**：对所有"新位置与旧位置不同"的资源，先按新位置 raw 写盘到最终文件名
    （此时旧文件仍在，设备仍在线；同名同资源更新除外——该情况在提交段写并带备份）。
    序列化 / 建目录 / 写盘 / fsync 任一失败 → 删除本次预写文件、清理暂存目录、报错。
    这样"资源写盘失败"绝大多数在切换前暴露。

### 阶段 B：一次切换（临界区提交；任一步失败立即回滚）

B1. 记录回滚快照：初始可见设备名集合（供事件差分）、旧树全部资源 dict 深拷贝及其
    (module, resource)、旧 `device_data_cache` 相关条目、暂存目录/备份目录清单。

B2. 关闭旧树：`close_device(旧父名)`（父 `close()` 级联关闭子设备）；
    对运行时中残留的旧前缀名再防御性 close。沿用现有 gc + 短 sleep 节奏释放标签/告警/硬件。
    **本步不发任何事件。**

B3. 配置目录就位：旧目录 `os.rename` 为同级 `.kaithem.bak.<uuid>`（跨文件系统时退回
    copy 语义），再把 A8 暂存目录 rename 为新目录。记录每一步用于回滚。

B4. 模块资源切换（只用 raw 原语，不触发 on_load/on_update）：
    - 搬迁的资源：A9 已写好新文件 → 直接更新/插入 `ActiveModules` 新位置
      （用不经写盘的内部放置，或带异常兜底的 raw_insert）；
      同位置资源：用内存旧值备份后 `raw_insert_resource` 覆盖写；
    - 随后从旧位置 `rawDeleteResource`（先插新、后删旧，保证失败时旧值仍可恢复）；
    - 自动创建/清理中间 `directory` 资源：新侧按需补齐（复用 `storeDeviceInModule`
      的建目录逻辑），旧侧被搬空的目录段逐层删除（仅当不再有其它资源以其为前缀），
      磁盘上对应空目录 best-effort `rmdir`。

B5. 缓存切换：删除旧树全部全名键，写入新树全部全名键 → (新module, 新resource, 新config)。
    必须在 B6 之前完成：父插件重建时 `get_config_for_device` 按新全名 pop 子设备配置。

B6. 运行时重建：严格模式 `makeDevice(新父名, 新config, 新module, 新resource)`：
    - 驱动缺失（`get_class` 查不到类）→ 维持历史行为，降级 `UnsupportedDevice` 占位；
    - **类可解析但 `__init__` 抛错（插件初始化失败）→ 异常向上触发回滚**，不再静默降级。
    - 父初始化时按新全名重建子设备并消费 B5 的缓存；插件未重建的子配置继续以占位形式
      留在缓存（与现有"先有子配置后有父设备"语义一致）。

B7. 事件差分：对比 B1 初始可见名集合与 B6 后可见集合：
    - 仅对最终消失的名字 post `/devices/removed/`；
    - 仅对最终新增的名字 post `/devices/added/`；
    - 同名改配置（集合不变）不发 add/removed；改名/迁移则旧树发 removed、新树发 added。
    - 顺序：先 removed 后 added。

B8. 清理：删除 B3 的 `.bak.` 目录、A8/B4 产生的临时文件与空目录；
    不主动 `recalcModuleHashes` 之外的额外动作（raw 原语已处理 hash）。

### 阶段 C：回滚（B2-B7 任一步抛异常）

C1. 关闭并清理已建立的新树运行时（按新前缀防御性 close + gc）。

C2. 配置目录：新目录删除（仅当由本次创建/替换），`.bak.` 目录 rename 回旧位置；
    A8 的暂存目录清空。旧设备在线期间其目录从未被删，最坏情况恢复到切换前快照。

C3. 模块资源：删除本次新插入的 (module, resource)（含新文件），
    将快照中的旧资源 dict 用 `raw_insert_resource` 放回旧位置（同时重写磁盘文件），
    使内存与磁盘都只剩旧版本；恢复/移除中间 directory 资源到快照状态。

C4. 缓存：从快照恢复旧全名条目，删除所有新全名条目（保证缓存中无旧/新并存、无新幽灵键）。

C5. 运行时：以旧配置严格重建旧父设备（`makeDevice` 旧名，子设备按旧缓存重建）。
    即使旧插件重建也失败（极少见），磁盘/缓存/资源仍为旧版本，重启后从磁盘收敛；
    记 critical 日志。

C6. 不发任何 add/removed 事件（最终可见集合 == 初始集合）；把原始异常继续抛给页面
（现状即为异常显示）。

### 阶段 D：重启收敛语义

- 提交成功：磁盘只有新 (module, resource) 文件，重启 on_load 只看到新全名。
- 提交中止：C3 保证磁盘只剩旧文件（预写文件已删、旧文件已重写恢复）；
  即使进程在回滚中被杀（极端断电，不在本次事务承诺内），由于"先插新后删旧"，
  磁盘可能短暂新旧并存——重启时现有 `load_closure` 的 "shares name" 检查会把它标为
  资源错误而不是静默加载两份；不新增重复设备。
- 运行期 `deferred_loaders` 不会再入队旧名闭包；缓存切换保证无旧名条目。

## 子设备直接编辑分支

`updateDevice` 现有 subdevice 分支（设备本身是子设备，L1152-1164）保留语义
（原地 `update_config`、不支持给子设备改全名），但同样：JSON 解析/校验前置，
资源移动与缓存写入带快照回滚，失败恢复原 module/resource 与目录。

## Implementation Steps

1. `devices.py`：抽出模块内辅助函数（均为下划线私有，不新增公开 API）：
   - `_enumerate_device_tree(devname)`：合并运行时 + 缓存，返回有序树条目；
   - `_map_child_resource(...)`：A6 路径映射；
   - `_stage_config_dirs(plan)` / `_commit_config_dirs` / `_rollback_config_dirs`；
   - `_prepare_resource_writes(plan)` / `_commit_resources` / `_rollback_resources`；
   - 一个内部 `_switch_plan` 数据结构（可用简单 dataclass/dict，放在同文件）。
2. `makeDevice(..., strict_init: bool = False)`：严格模式下类可解析则让初始化异常直接
   抛出；缺驱动仍降级 `UnsupportedDevice`。默认参数保持所有其它调用点行为不变。
3. 重写 `updateDevice()`：A1-A9 准备 → B1-B8 提交 → C1-C6 回滚（try/except 包裹提交段）；
   子设备分支改为前置校验 + 回滚保护。
4. 保持锁的用法不变（全程 `modules_state.modulesLock`，RLock 可被
   `on_config_changed` 同线程重入）；不在锁内新增用户回调；事件发送点与现状一致在锁内。
5. 新增测试 `kaithem/src/tests/test_devices.py`：
   - 无效 JSON 改名失败：旧设备仍在、旧标签/告警仍在、磁盘 yaml 仅旧版本、
     缓存无新名、事件计数为 0；
   - monkeypatch 注入初始化即抛错的设备类型：严格重建失败后旧设备在线、旧资源/目录恢复；
   - monkeypatch `shutil.copytree` 失败：旧设备与旧目录完整；
   - monkeypatch 资源写盘在提交段失败：旧设备在线、ActiveModules/磁盘唯一旧版本、
     缓存与运行时无重复；
   - 成功改名：父+子新全名运行、子配置（`device.echo_number`）保留、旧名标签消失/
     新名标签存在、旧资源文件与旧 `.config.d` 消失、新位置存在、事件 removed/added
     各与最终可见集合一致、缓存无旧名；
   - 成功跨模块迁移：父+子资源与配置目录搬到新模块，旧模块无残留；
   - 同名保存配置：无 add/removed 事件、配置生效；
   - 锁与冲突：目标模块锁 / 资源锁 / 新名已被他设备占用 / 设备类型变更 均被拒且零副作用；
   - 中止后重新从磁盘装载（直接读取模块目录 yaml 重建 `ActiveModules` 视角或调用现有
     加载路径）只得到旧全名唯一版本。
6. 静态检查：`python3 -m py_compile kaithem/src/devices.py kaithem/src/tests/test_devices.py`；
   如本机有 ruff 则 `ruff check`；尽力在 Linux Docker（`make dev-run-all-tests` 相关
   pytest 子集）中运行 `test_devices.py`，若环境不可用则在交付说明中注明。

## Dependencies and Considerations

- iot_devices API 以 0.34.0（pyproject 要求 >=0.34.0）为准：`close_device` 级联、
  `add_device_from_class` 失败会自动移除空容器并 re-raise、`create_subdevice` 命名规则。
- 配置目录在跨外部模块位置（`external_module_locations`）时可能跨文件系统，
  rename 失败要退回 copy；暂存目录放在目标侧同级以尽量保证同 fs。
- 插件在 `__init__` 中可能主动 `on_config_changed` 写资源（同线程 RLock 安全），
  提交段其 module/resource 已是新值，与现状一致。
- 不改变缺驱动降级占位的历史行为（管理页可继续编辑修正），只把"类存在但初始化崩溃"
  改为触发回滚——这是本次需求明确点名的失败模式。
- 不承诺抵御掉电级崩溃（无 WAL）；掉电后由现有同名冲突检查兜底，不加载重复设备。

## Validation

- 新增 pytest 全绿（在 Linux/Docker 测试环境）；
- 现有 `test_devices.py` 三条测试不修改断言且继续通过
  （尤其子设备 echo_number 跨父更新保留、删除后标签与缓存行为）；
- py_compile / ruff 无新增告警；
- 手工走查：改名、跨模块迁移、同模块改资源名、仅改配置、子设备单独编辑 五条路径
  在注入 JSON/复制/写盘/初始化 四类故障后状态一致。

## Risks

- **风险：长临界区 / gc sleep 增加保存延迟。** 缓解：预拷贝与预写盘在准备段完成，
  提交段只做 rename/内存操作；sleep 量级沿用现状（0.01s 级）。
- **风险：自定义子资源路径在跨模块迁移时的映射与用户预期不同。** 缓解：同模块保持
  原路径（兼容现有测试与 UI）；仅跨模块时迁入约定 `.d/` 目录，冲突前置报错。
- **风险：回滚重建旧设备也失败。** 缓解：保证磁盘/缓存/资源先恢复到旧版本
  （重启可收敛），并记录 critical 日志、向页面抛错；不静默假装成功。
- **风险：插件在初始化中有不可撤销的外部副作用（发网络请求等）。** 这是插件本身职责，
  框架层无法回收；维持"先关旧再建新"的现有硬件模型，不引入双实例并发。
