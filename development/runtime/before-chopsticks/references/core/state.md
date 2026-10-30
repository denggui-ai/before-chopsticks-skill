# 本地状态与基础修图

本参考适用于项目初始化、导入、版本选择、修图、恢复和宿主执行准备。CLI只处理本地数据与文件，不调用外部模型。需要Python 3及Pillow，锁依赖`fcntl`；当前验证环境为macOS，其他系统尚未验证。命令中的Skill路径、项目路径及ID由实际位置和返回值填入，不凭文件名猜ID。

## 调用与结果

```bash
python3 <skill-dir>/scripts/state.py --project <project-dir> init --name 晚餐
python3 <skill-dir>/scripts/state.py --project <project-dir> inspect
python3 <skill-dir>/scripts/state.py --project <project-dir> apply --input command.json
python3 <skill-dir>/scripts/state.py --project <project-dir> recover
```

`command.json`为UTF-8 JSON对象，也可用`--input -`从标准输入传入。常规返回`{ok:true,result:...}`；参数错误、blocked、protocol_error或降级恢复返回`ok:false`及退出码2。先读result/error再决定下一步；退出码0不意味着生成成功。`inspect`只读，`recover`修复本地可证明状态，不查询或重发外部调用。

## 单张本地循环

1. `{"action":"create_work","subject":"牛肉面"}`返回Work；记录其`id`。
2. `{"action":"import_image","work_id":"<work-id>","path":"<absolute-image-path>"}`复制并校验原图，返回Version/Asset，设置编辑目标。导入不伪造外部Attempt。
3. 展示某版只读取`project.versions[version_id]`及其Asset；`locator`相对项目目录。先验证实际文件存在且与记录的SHA-256一致，再展示。
4. `{"action":"set_edit_target","version_id":"<version-id>"}`用于“回这版继续”；`select_version`用于用户明确选中，且同时设置编辑目标。只展示不调用两者。
5. 调色示例：

```json
{
  "action": "local_edit",
  "version_id": "<version-id>",
  "mode": "append",
  "operations": [
    {"type":"brightness","params":{"factor":1.1}},
    {"type":"warmth","params":{"amount":0.08}}
  ]
}
```

返回新Version、规范化管线和warnings，原件保留。新操作可省id，由核心分配；已有操作保留稳定id。`append`从所选分支的master重放已有操作再追加，操作列表不能为空；`replace`以该master及传入完整操作列表重放，用于调整/删除已有步骤，`operations:[]`清空全部步骤并生成该master的新版本。不要把“再加一点”误写为全管线替换。

master/蒙版/原引擎不可用时，核心可将当前可读图设为新master并警示不可无损回退。新操作须按该图坐标定义；旧mask不能凭相同尺寸复用。当前版本本身不可读则拒绝处理，不猜邻近文件。

`{"action":"export_version","version_id":"<version-id>","image_format":"png"}`另存导出版，保留当前编辑目标；支持png/jpeg/webp/tiff。无损导出保留可重放管线；有损导出后继续编辑以其实际像素为新master，并提示旧处理不可无损撤回。导出结果在项目assets内，交付其真实路径；另复制到用户指定位置时按实际文件权限操作。

## 候选标签与换菜

`create_group`接受`work_ids`及可选`kind`；同一菜品的候选可共用Work，每次准备须固定`group_id,item_id,work_id`。新输出同时保存数字标签如`2-1`及字母别名`2-a`；第二个结果为`2-2`/`2-b`，重试分配新标签，已交付键不覆盖、不重编号。普通Work版本仍可用`V1`等标签。

`{"action":"resolve_version_label","label":"2-b","group_id":"<group-id>"}`只读解析，返回`version_id,work_id,group_id,item_id`；`select_label`接受相同参数，按`select_version`规则更新选中与编辑目标并增加epoch。可显式给`work_id`，或使用`<group-id>:2-b`等完整标签；省略范围时使用活动组/作品，不扫描最新结果兜底。项简写`"2"`只有对应一个Version时才可解析；多版本时拒绝歧义，须选择具体标签。

开始新的多项组图上下文会恢复`ambiguous`，即使此前选过某版；一个子调用只返回一张也不自动设为编辑目标。用户明确选定后，普通后续编辑可使用该目标；新的组图编辑仍需逐项明确父版。晚到结果、恢复补接纳及`latest`变化不改绑已选标签。

换菜用`create_work`的`subject,borrowed_from,borrowed_keys`建立新Work。`borrowed_from`须是明确可读的Version；只复制其快照中被`borrowed_keys`点名且`dependencies`不含`subject`的条件，重新限定为新Work，其他候选分支不会混入。旧Version不改挂，也不自动成为新Work父版；若用旧图作新作品编辑底图，先导入新Work形成其自己的Version。

## 支持的确定性操作

仅渲染单帧8-bit RGB/RGBA，应用EXIF方向后使用显示坐标，保留alpha及已有ICC。操作按数组顺序执行；裁切会改变后续坐标。

| type | params | 约束 |
|---|---|---|
| brightness / contrast / saturation | `factor` | 0–4的有限数，1为不变 |
| warmth | `amount` | -1–1的有限数，0为不变；通道增益调色，不宣称物理白平衡 |
| crop | `box:[left,top,right,bottom]` | 当前帧内整数像素，右/下边界不包含在结果内 |

局部brightness还需`mask_asset_id`和`params.mask_source:{master_sha256,geometry}`。mask先用`import_asset`导入，必须是该操作输入坐标中的1/L单帧图；白应用、黑保留、灰混合。`geometry`是从master至此的完整裁切序列，例如`[{"type":"crop","box":[1,0,9,8]}]`。该声明必须有真实来源依据；程序核对哈希与几何链，不验证“这确实是食物区域”。可选`input_frame/output_frame:{width,height}`和`engine_version`若提供必须匹配。JPEG会有损且拒绝丢弃alpha。

## 要求、参考与检查

长期适用要求用`add_requirements`：`records:[{key,value,source?,dependencies?}]`，`scope:{kind:"project"|"work"|"group",id,...}`，`message_id`指向真实用户消息。默认来源为`user_explicit`；AI设计/默认须分别标`ai_design`/`default`。对本次生成分支的反馈放`prepare.changes`，不使用全局共享要求代替。

`import_asset`只登记素材，不创建作品版；`add_reference`接受`asset_id,roles,scope`。每个role含`role,borrow,exclude,strength,source`；例如借光线、排除菜品，strength为required/preferred/informative。角色支持base_image、subject、food_styling、action、composition、lighting、color、material、style、continuity。只借`borrow`点名属性，不把`exclude`内容带入；来源与角色随请求快照保留。

Reference的`scope`支持`project/work/group/item`及对应`id`，也可用`item_ids`进一步限定范围；添加时验证归属，准备与提交前再次核对目标。显式引用范围不匹配会报错，不会静默忽略或扩到其他作品。引用通过`prepare.references`显式传Reference ID数组，不依赖素材目录排序。Codex桥仅在语义编辑中使用多参考，总输入最多三张、包含一个主底图；新生成不带参考。

`record_read_only`接受`work_id,intent,design`；intent为explore/critique/shoot_guidance，返回`attempt:null`。只在需要项目记录时调用，不为普通问答强建项目。

`record_check`接受`version_id,category,status,evidence,note?`，保存独立检查记录；category为artifact_integrity/requirement_checks/visual_observations/encoding_check，status为pass/fail/unclear/not_applicable。evidence必须有内容且可追溯。检查与用户选择分开，选版不把fail改成pass。

## 外部执行接口与恢复

以下是共同核心的持久化接口。Codex原生调用使用已接入的[Codex图像桥](../hosts/codex.md)准备参数与保存真实回执；这些核心方法本身不调用模型。

- `authorize`记录`work_ids,allowed_operations,requested_count,retry_budget,paid_channel_scope,evidence`。操作限generate/generative_edit，渠道为明确adapter ID列表。授权必须来自实际用户指令，不以设计建议充当。增加重试用`extend_retry_budget`及新增授权证据。
- `prepare`接受`work_id,authorization_id,adapter,capabilities,operation?,parent_version_id?,changes?,references?,design?,group_id?,item_id?,begin_context?`；返回固定Request和prepared或failed/not_submitted的Attempt。`operation.expected_outputs`默认为1，进入次数计量。`generative_edit`省略父版时只读取已明确的编辑目标；`ambiguous`或无可读父版会阻塞。
- `dispatch`接受`attempt_id,capabilities?,input_reference_map?`，锁内复查输入及全部Attempt占用额度。返回`reprepared`时旧Attempt已取消且未提交，使用新ID；`blocked`停止受影响调用；`dispatching`只证明提交意图已保存。不得重复dispatch同一Attempt。
- `observe`接受`attempt_id,state,submission,evidence,outputs?,handle?,tool_call_id?,auto_target?`。每个output须有稳定`output_index,path,provenance`；provenance含经实际核验的`attempt_id,verified:true`及非空`source`来源说明，有tool_call_id时须一致。`verified:true`是宿主核验后的事实声明，不是绕过核验的开关。恢复接纳使用`auto_target:false`，保留用户编辑上下文；普通活跃回执默认true，仍受epoch/归属条件检查。
- `retry`只处理failed/cancelled/unknown；unknown另需`duplicate_risk_ack`，仍受现有重试预算约束。没有新增明确授权和重复风险确认，不能为unknown自动另起调用。
- `cancel_prepared`只能取消确定未提交的准备项；提交后的取消必须有外部确认，不能从“已请求取消”推导cancelled。
- `acknowledge_unknown`可记录用户停止追查的决定，不能清除unknown、释放额度或伪造成功。

权威用户状态位于`state/revisions/`完整校验链，`project.json`是镜像；Request不可变，Attempt独立保存执行事实。恢复时镜像损坏可重建，正式链缺失/损坏则报告降级并暂停新写入。输出接纳键保证补接纳同一输出一次，恢复不重发调用、不改用户当前编辑目标。不要删除Attempt或手改revision来“解锁”。

## 系列锚点与依赖

`set_series_anchor`接收`group_id,version_id,borrowed_roles`，仅适用于visual_series/narrative_series，版本须可读且属于系列Work，生成版本来自成功Attempt。设置组锚点只提供可借鉴版本，不自动让所有项依赖它。

`set_item_anchor`显式声明某项的使用：`group_id,item_id,reference_ids,follow?`。新建项的`anchor_binding:null`表示独立；`reference_ids:null`也可将旧项明确设为独立。数组表示依赖当前组锚点，`[]`只保存逻辑借鉴、不附图；要附图则提供承担锚点借鉴的Reference ID，必须是当前锚点Asset、适用本项、角色属于borrowed_roles且不是base_image。准备时仍须在`references`显式提交这些ID，不能遗漏。`follow:true`（默认）在未提交项重准备时跟随新组锚点；`follow:false`固定绑定时的具体Version，不因组换锚点变化。

绑定引用解析为带`anchor_reference_id/anchor_version_id`来源的派生快照（ID为原Reference ID加`@Version ID`），实际Asset随明确绑定解析，原Reference和旧Request不改。新锚点角色与引用冲突则阻塞本项，不能扩大借鉴。缺锚点素材只阻塞绑定项；普通参考仍按自己的素材校验，不因组锚点换图而改变。旧项目缺anchor_binding且存在组/已准备锚点时，使用意图不明，需通过set_item_anchor明确；不猜成全独立或全依赖，不手改历史快照。

`set_item_dependencies`仍接受`group_id,item_id,dependencies`，每项为更早组项的item_id及具体version_id；未产生版本可置null，受影响项阻塞，得到成功版本后明确绑定。这类固定工序依赖不会随组锚点替换。失败或unknown候选不能冒充成功依赖。相关输入变化只重准备确定未提交项；dispatching/unknown/已提交Request冻结、不自动重发、不释放未知额度。

历史动作、镜头偏差和跨题材稳定性限制仍保留；接受某一作品不等于所有系列通过。远程服务中断恢复未获查询/取消能力保证。

普通几道菜各一张、没有共享风格要求时使用 `independent_items`；不同创意供选用 `candidates`；共享指定摄影条件用 `visual_series`；按时间与食材状态递进用 `narrative_series`。只记录用户要求的共享条件。
