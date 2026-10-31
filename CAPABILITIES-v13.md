# v13 能力清单（2026-10-31）

本清单说明v13开发候选的运行入口、已有验证及限制，对应提交 `e2adb06`；此后的改动见 [CHANGELOG](CHANGELOG.md)，目前只有一条画幅记录规则和一项对应离线回归。运行文件位于仓库的 `development/runtime/before-chopsticks`，具体安装见 [README](README.md)。v13本身不证明已部署到使用者环境，也不代表production-ready。

**已有验证**分为离线程序/像素检查、模型实际读取后的文字行为、真实图像案例。较早版本的实图与安装记录只在原案例及版本范围内有效；v9/v10/v11文字回归不能拼接成v13完整端到端通过。下表E1—E10为验证摘要编号，文末解释范围；私有原始证据、用户作品和原始调用记录不随本清单提供，未公开的验证摘要不等于读者已能独立复核全部实验。

## 创作与交互

| 能力 | v13运行入口 | 已有验证 | 未验证与限制 |
|---|---|---|---|
| 理解菜品、照片与想法 | [SKILL](development/runtime/before-chopsticks/SKILL.md)；[photography](development/runtime/before-chopsticks/references/core/photography.md)「任务入口与推进」、P-FOOD | E1有真实菜品单图及编辑；E4有无图设计与缺图行为 | 不证明自动识别任意菜品；缺图不能声称看见具体结构、曝光或配料 |
| 找方向、指定数量、推荐 | [photography](development/runtime/before-chopsticks/references/core/photography.md)「探索方式」；[interaction](development/runtime/before-chopsticks/references/core/interaction.md)「创意探索与图组选择」 | E5：一次隔离会话实际完成指定四方向等四项文字任务；E4有三影调方案 | 数量遵循有文字证据，不代表任意数量图像均已生成验证 |
| 混选故事、光线与删去元素 | [interaction](development/runtime/before-chopsticks/references/core/interaction.md)「创意探索与图组选择」 | E3补齐上下文后完成混选；E5组合故事与光且去手 | 只验证文字组合；未证明混选描述在实际成图中全部落实 |
| 故事时刻、食材来处与过程 | [photography](development/runtime/before-chopsticks/references/core/photography.md)「动作」「候选与系列」、P-FOOD；[interaction](development/runtime/before-chopsticks/references/core/interaction.md)「创意探索与图组选择」 | E4有来处至食用的六张文字规划；E2有真实八张制作系列及修正 | 来处是可选设定，非调查事实；跨题材工序与精确时刻未普遍验证 |
| 食物形态、质感与器皿 | [photography](development/runtime/before-chopsticks/references/core/photography.md) P-FOOD、P-CHECK、「动作」 | E9汇总酥层、切面、透明结构及倒液样本；E1有菜品保留观察 | 倒液后杯形、比例与位置只部分满足；复杂液体和精确结构未稳定验证 |
| 场景、器物与空间关系 | [photography](development/runtime/before-chopsticks/references/core/photography.md) P-SPACE、P-FOOD、「动作」；[interaction](development/runtime/before-chopsticks/references/core/interaction.md)参考环境规则 | E2有厨房/器物系列；E8指定背景、台面样本改善 | E8木质筷子仍违反整条无木材要求，不能称场景解耦全面通过 |
| 影调 | [photography](development/runtime/before-chopsticks/references/core/photography.md) P-TONE | E4三档影调文字；E9汇总三档实图及后续画幅/影调文字任务 | 主体、背景与面积须分别看；不同方案连带改变构图的案例不证明参数严格独立控制 |
| 光质、光向与受光联动 | [photography](development/runtime/before-chopsticks/references/core/photography.md) P-LIGHT、P-CHECK | E6真实复测有主体受光改善，但整体partial/unclear；E4有实际提交遗漏归因文字回归 | 盘内外投影证据仍不足；不承诺整图物理一致性，不由单例推断模型硬上限 |
| 色彩、冷暖与风格 | [photography](development/runtime/before-chopsticks/references/core/photography.md) P-COLOR；[interaction](development/runtime/before-chopsticks/references/core/interaction.md)「创意探索与图组选择」 | E3有亮调怀旧；E4有光色独立设计；E9汇总原光色样本 | 通用怀旧、年代记忆保留，不绑定暗暖或旧器物；跨风格/题材视觉稳定未证实 |
| 画幅、取景与清晰范围 | [interaction](development/runtime/before-chopsticks/references/core/interaction.md)单图画幅与影调记录；[photography](development/runtime/before-chopsticks/references/core/photography.md) P-SPACE | E9：FRAME01新建、FRAME02编辑继承各一次隔离正式无图prepare/preview；E2八张实际3:4 | 原生接口无精确尺寸参数；混合画幅、任意比例和机位准确性未普遍验证；目标比例与实图分开；只说横幅/竖幅时的记录规则在 `751070f` 中修改，E9 未按新规则重跑，另有一次无图准备与一项离线回归 |
| 动作、人物与露脸选择 | [photography](development/runtime/before-chopsticks/references/core/photography.md)「动作」；[interaction](development/runtime/before-chopsticks/references/core/interaction.md)系列人物规则 | E2有限八张不露脸系列获用户接受，有时序/动作定向修正；E4人物自主设计文字方案 | 用户接受不抹去刀刃时刻、下锅机位偏差；手部不能证明身份连续；不设全局不露脸默认 |
| 模糊反馈与多项修改 | [interaction](development/runtime/before-chopsticks/references/core/interaction.md)「模糊反馈与连续修改」；[photography](development/runtime/before-chopsticks/references/core/photography.md) P-CHECK | E9 M3限定通过，含真实编辑；E3/E5有反馈和组合文字证据 | 不能保证长时间多轮永不漂移；没有照片时只能提出条件式建议 |
| 点评 | [photography](development/runtime/before-chopsticks/references/core/photography.md) P-CRITIQUE | E9 M4有实际看图点评；E4有缺图不编造观察的文字回答 | 不能把观察推断当真实灯位/器材鉴定；点评不自动修图或生成 |
| 实拍指导 | [photography](development/runtime/before-chopsticks/references/core/photography.md) P-SHOOT | E9有旧安装新会话手机指导及用户视角任务；E5有手机加左窗指导 | 限指导交付；用户实际拍摄是否改善未验证，不因给出建议宣称实拍成功 |

## 版本、图组与工具执行

| 能力 | v13运行入口 | 已有验证 | 未验证与限制 |
|---|---|---|---|
| 查看、选版、回退继续与分支保留 | [state](development/runtime/before-chopsticks/references/core/state.md)「单张本地循环」「候选标签与换菜」；`scripts/state.py` | E1恢复保留用户目标；E3合成项目核对回第一版另开分支、第二版仍在；E7离线回归 | “看第一版”不切目标；回退不是抹除历史；语义改图仍可能漂移 |
| 独立候选、稳定标签与消歧 | [state](development/runtime/before-chopsticks/references/core/state.md)「候选标签与换菜」；`create_group/resolve_version_label/select_label` | E9保留P3实际选非latest后编辑范围；E7含标签、归属、晚到结果及消歧回归 | 不用最后一张猜对象；单次子结果不代替用户选版；候选差异质量仍逐张判断 |
| 多菜独立与换菜继承 | [interaction](development/runtime/before-chopsticks/references/core/interaction.md)「创意探索与图组选择」；[state](development/runtime/before-chopsticks/references/core/state.md)「候选标签与换菜」 | E5三菜独立场景文字；E7要求/锚点隔离回归；E9原换菜实测范围 | 不强制统一场景；不证明任意多菜一轮自动完成；只继承点名且不依赖旧主体的条件 |
| 统一视觉系列与过程叙事系列 | [photography](development/runtime/before-chopsticks/references/core/photography.md)「候选与系列」；[state](development/runtime/before-chopsticks/references/core/state.md)「系列锚点与依赖」 | E2真实八张系列；E9锚点引用及按项依赖修复范围通过；E7相关离线回归 | 张数不固定；镜头、份量、人物和空间连续性未普遍稳定；不把有限系列接受扩大到全部系列 |
| 参考角色、借鉴/排除及输入顺序 | [interaction](development/runtime/before-chopsticks/references/core/interaction.md)参考规则；[state](development/runtime/before-chopsticks/references/core/state.md)「要求、参考与检查」；[codex](development/runtime/before-chopsticks/references/hosts/codex.md) | E8真实三参考场景修正部分满足；E7角色映射、哈希、缺失输入和锚点绑定回归 | 本适配器编辑最多三张含主底图，新生成不带参考；语义属性解耦不保证，不能称像素保护 |
| 按范围更新要求与保留条件 | [state](development/runtime/before-chopsticks/references/core/state.md)「要求、参考与检查」；`requirements.py` | E3有比例替换且暗调/主体保留的离线夹具；E7要求归并、分支与冲突回归 | 规则解析通过不等于最终图像满足；冲突只阻塞受影响对象，不自动丢掉保留项 |
| 授权、原生生成与语义编辑 | [SKILL](development/runtime/before-chopsticks/SKILL.md)；[codex](development/runtime/before-chopsticks/references/hosts/codex.md)；`scripts/codex.py` | E1真实生成1次、编辑1次；E9后续本地首版有限调用；E7执行/额度边界回归 | 真正调用由宿主执行，CLI只准备/归档；尺寸/费用unknown；工具可用性须按当次声明，不保证完整新会话自动出图 |
| 回执归档、中断恢复与防重发 | [codex](development/runtime/before-chopsticks/references/hosts/codex.md)「真实回执与输出」「中断恢复」；[state](development/runtime/before-chopsticks/references/core/state.md)「外部执行接口与恢复」 | E1真实回执落盘后进程退出、重启接纳与二次恢复幂等；E7崩溃窗口/unknown/晚到结果回归 | 仅本地可证明恢复；远程未返回或无回执仍unknown，不自动重发；无远程query/cancel或恰好一次保证 |
| 分层检查、作品接受与停止 | [SKILL](development/runtime/before-chopsticks/SKILL.md)「完成闸门与能力状态」；[state](development/runtime/before-chopsticks/references/core/state.md) `record_check/select_version` | E9 M8分层验收；E2作品user-approved；E7不把选择当工具成功的回归 | 工具成功、文件可读、要求满足、视觉接受和安装发布互不替代；未观察保持unclear |

## 本地修图、导出与安装边界

| 能力 | v13运行入口 | 已有验证 | 未验证与限制 |
|---|---|---|---|
| 亮度、对比、饱和、暖色及裁切 | [state](development/runtime/before-chopsticks/references/core/state.md)「支持的确定性操作」；`state.py apply`（`action=local_edit`）、`bc_core/local_edits.py` | E7的146项整体回归包含参数、顺序、裁切坐标及原件保护测试 | 当前仅`brightness/contrast/saturation/warmth/crop`；不是完整RAW曝光、物理白平衡或完整曲线/HSL工具 |
| 显式蒙版局部亮度 | [state](development/runtime/before-chopsticks/references/core/state.md) `mask_asset_id/mask_source`；`local_edit` | E7含蒙版外像素、alpha、master哈希和几何链检查；E9保留历史局部提亮范围 | 只支持有来源依据的显式蒙版，不具备自动食物分割；程序不判断“这确实是食物区域” |
| 操作重放、删步骤与清空 | [state](development/runtime/before-chopsticks/references/core/state.md)「单张本地循环」；`local_edit(mode=append/replace)` | E7 v13新增两项回归：`replace+[]`还原master像素，直接版/无损导出后均验证，旧分支保留；空append/非列表不写状态 | master/旧mask/引擎不可用时明确降级为当前可读图；不能承诺撤回此前不可重放处理 |
| PNG/JPEG/WebP/TIFF导出与颜色信息 | [state](development/runtime/before-chopsticks/references/core/state.md) `export_version`；`local_edits.py` | E7编码重开、像素、ICC及适用alpha测试；有损导出后续编辑警示回归 | 单帧8-bit RGB/RGBA；高位深/多帧和不支持模式拒绝；JPEG有损且不能丢alpha，完整打印/色彩工作流未验证 |
| 超出基础操作的修补与增强 | [SKILL](development/runtime/before-chopsticks/SKILL.md)本地/生成式边界；[codex](development/runtime/before-chopsticks/references/hosts/codex.md)语义编辑 | E1只有整图语义改色等有限案例，不能充当专门局部工具证据 | v13没有确定性旋转/透视、曲线/HSL、锐化/降噪/超分、去污/克隆、精细抠图、RAW或批处理接口；改背景/扩图等只能在实际工具支持时讨论语义编辑，不列为已验证专门能力 |
| 发现、正式无图准备与安装恢复 | [SKILL](development/runtime/before-chopsticks/SKILL.md)；[codex](development/runtime/before-chopsticks/references/hosts/codex.md)；公开README/指南 | E10旧版单次新会话自然发现→读取→正式prepare/preview；E9旧安装实际发现、指导、移走原目录后不发现、移回后重发现，FRAME01/02有限无图证据 | 这些不是v13新安装验收；独立备份恢复/旧作品迁移未演练，完整新会话自动出图未验证 |
| 平台与创作范围 | [SKILL](development/runtime/before-chopsticks/SKILL.md)；[state](development/runtime/before-chopsticks/references/core/state.md)依赖；公开README | E1/E9有本地Codex、macOS限定运行；E7有本机离线测试 | 需Python/Pillow/fcntl与可用原生图像工具；Linux未验证，原生Windows不承诺；Claude不在v13运行范围；微缩/超现实/插画等未按稳定能力验收 |

## 验证摘要与可复核范围

- **E1｜真实单图及本地恢复**：较早开发版真实生成一次、编辑一次，并验证回执可靠保存后的进程退出和重启接纳；不代表v13重新跑过完整图像链。
- **E2｜有限系列案例**：八张过程系列有真实输出和用户接受记录，仍保留动作时刻、机位与身份连续性的限制；作品和原始记录不随仓库提供。
- **E3｜创意与连续修改**：文字任务涵盖方向、亮调怀旧、混选和独立多菜；另有本地合成夹具验证回退分支和要求替换。文字回答与程序执行分开理解。
- **E4｜v9/v10文字回归**：有限任务覆盖影调、改光描述、过程规划、缺图点评和提交遗漏归因；有实际技能读取，不代表隐式发现或新成图验证。
- **E5｜v11文字回归**：一次隔离会话完成四方向、故事与光混选且去手、三菜独立场景、手机加左窗指导四项任务；不是四次独立会话。
- **E6｜改光实图复测**：主体受光有改善，整图投影一致性仍记为部分达到/不确定，不升级为整体通过。
- **E7｜v13离线回归**：整套146项通过，含两项新增清空操作与输入拒绝回归；146不是表中每项各有146例。`751070f` 新增一项画幅方向记录回归，当前共147项。仓库保留可运行的[测试目录](development/evidence/tests)，其中有[状态与版本测试](development/evidence/tests/test_engine.py)、[本地修图测试](development/evidence/tests/test_local_edits.py)、[锚点绑定测试](development/evidence/tests/test_anchor_binding.py)。运行方式见README；测试使用合成夹具，不调用图像服务。评测定义本身不算执行结果。
- **E8｜三参考场景修正**：指定背景、台面有所改善，但道具材质仍有违例；参考解耦只部分满足。
- **E9｜较早本地首版限定验收**：包含有限光色、编辑、点评与实拍指导、锚点隔离、分层检查，以及安装后使用和原目录移回恢复。新建和编辑各有一次画幅/影调正式无图准备；独立备份恢复未演练。这些不算v13新安装验收。
- **E10｜较早正式无图准备**：单次新会话自然发现、读取技能并实际执行prepare/preview；止于准备，没有dispatch或原生图像调用。

本清单不宣称完整自动新会话出图、跨题材普遍稳定、长期编辑永不漂移、像素级语义保护或全部安装生命周期已验证。验证摘要不提供版权或第三方授权结论。
