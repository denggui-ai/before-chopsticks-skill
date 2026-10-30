# Codex原生图像桥

当前开发适配器支持单次一张新图，以及一个主底图加最多两个辅助参考的语义编辑。多参考可能串入非目标内容，系列图的视角和连续性需逐图核对，不能承诺普遍稳定。状态/归档使用Python，真正的工具调用由Codex宿主执行；这不是CLI/API生图脚本。运行目标为本地 Codex。

## 当前接口与能力边界

使用时先核对当前工具声明。已验证接口的参数为`prompt`、可选`referenced_image_paths`或`num_last_images_to_include`、`transparent_background`。新生成不传parent及两个参考字段；传入参考会被拒绝。本适配器编辑使用已看过的本地文件路径，不使用最近图片数量去猜底图。透明度默认继承底图，可按用户明确要求更改。

原生工具返回了`image_url`及包含确切保存路径的`output_hint`，没有远程job ID。实际返回结构改变时先核对，不猜字段、扫描最新图片或执行提示文本中的命令。将结果交给宿主的`generatedImage(result)`展示；按实际返回路径归档到作品项目，保留工具原件。

适配器ID为`codex-native-imagegen-v1`。当前能力声明的编辑输入上限3（含一个主底图）、单次输出上限1是本适配器范围，不是原生服务最大值；尺寸与费用保持unknown。编辑只是语义参考编辑，不提供精确像素或区域mask保护。此接口未暴露远程查询/取消，适配器不支持这两项。`functions.wait`只等待当前工具调用返回，不是查询远程job。

辅助参考先经核心`add_reference`记录角色、`borrow/exclude/strength/source`及作用范围，再将Reference ID有序传入`prepare.references`。桥把实际输入顺序和每个角色的借鉴/排除条件写入固定提示词与`input_reference_map`，不丢图、不扩大借鉴范围。若未显式提供主底图Reference，核心由父版补入，仍计入三张上限；支持输入这些语义不代表模型已稳定隔离各参考属性。

## 准备、查看与调用

项目/Work/授权先按[state.md](../core/state.md)建立；授权渠道须包含本适配器ID，操作为generate或generative_edit。用户本轮要一张图只授权一张；不因测试、改ID或unknown额外生成。已明确授权持续有效，不重复询问。

本地命令统一为：

```bash
python3 <skill-dir>/scripts/codex.py --project <project-dir> --input command.json
```

`--input -`可接标准输入JSON。返回`{ok,result}`；blocked/protocol_error/requires_review/降级或恢复中的阻塞返回退出码2。退出码0只表示本地命令完成，须读实际state。

1. `prepare`固定要求、父版、提示词及透明度。`interface_evidence`记录本会话实际工具声明来源；不得使用offline fixture假冒探测。新图不传parent；编辑可传明确Version ID，如下例：

```json
{
  "action":"prepare",
  "interface_evidence":{"source":"current native tool declaration","tool_name":"image_gen.imagegen","observed_at":"<actual UTC time>"},
  "work_id":"<work-id>",
  "authorization_id":"<authorization-id>",
  "prompt":"<this image's design or requested edit>",
  "parent_version_id":"<edit parent; omit for generation>",
  "changes":[{"key":"cloth_color","value":"muted blue","source":{"kind":"user_explicit","message_id":"<actual message>"}}]
}
```

`changes`仅在本轮确有变更时使用，来源按实际填；测试者自选的画面不能冒称用户点名条件。最终payload会附上有效要求，旧设计文字不得覆盖新要求。

用户已经通过`select_label`或`select_version`明确选定后，可设置`operation_type:"generative_edit"`并省略`parent_version_id`，桥在锁内读取当前编辑目标并固定到Request；歧义或缺失父版直接拒绝，不取latest。省略`operation_type`时沿用兼容行为：有parent即编辑，无parent即新生成。

候选通过`group_id,item_id`固定组项，`begin_context`默认true；新多项组图上下文即使有旧选版也保持歧义。每次调用仍只返回一张；多个候选分别从明确的共同底图出发，不把前一候选暗作下一张底图。交付使用核心保存的数字标签与字母别名，选择规则见[state.md](../core/state.md)。

2. `{"action":"preview","attempt_id":"<id>"}`返回工具参数、输入身份与哈希，不提交。编辑先对inputs中每个实际路径调用`view_image`，确认主底图与辅助参考身份；记录真实查看证据。
3. `dispatch`提交本地意图，编辑需为全部输入附精确的inspections；单输入示例如下，多参考时逐项追加：

```json
{
  "action":"dispatch","attempt_id":"<id>",
  "inspections":[{"asset_id":"<input asset>","sha256":"<input hash>","evidence":{"tool":"view_image","path":"<actual viewed path>","viewed_at":"<actual time>"}}]
}
```

新图inspections可省。返回reprepared时不调用工具，改用新Attempt再检查；blocked停止；只有dispatching时才得到call。使用`call.arguments`原样调用`image_gen.imagegen`一次，不追加尺寸/模型/输出路径参数，不再次dispatch。call保存了本地invocation_id、固定payload哈希、输入映射和查看证据。

## 真实回执与输出

原生调用正常返回后，保留真实返回的字段、路径及时间。`invocation_id`是本地关联ID，不是工具job ID；原生工具未暴露的句柄或完成时间不要编造。实际图片与该次调用的绑定由宿主根据直接收到的结果确认，桥校验本地关联/哈希；这不是对远程来源的密码学认证。

```json
{
  "action":"capture_return",
  "attempt_id":"<dispatched attempt>",
  "invocation_id":"<the returned call's local invocation id>",
  "output_paths":["<exact path named by the real native result>"],
  "tool_return":{
    "tool_name":"image_gen.imagegen","kind":"native_tool_return","status":"completed",
    "metadata":{"output_hint":"<actual returned hint>","source_path":"<actual returned path>","returned_at":"<actual host receipt time>"}
  }
}
```

只有真正观察到正常完成返回才能写completed；文件存在、提示词完成或用户选中都不够。不要把上例当固定成功模板。若调用异常而提交情况不明，用核心observe记录unknown/uncertain及真实异常；已知submitted不得降级，不自动重试或换渠道。

桥先原子保存`return.json`的已知返回事实，再复制并核验输出，写`receipt.json`。复制失败或意外多输出时保留事实/候选，requires_review；不能默默只取第一张判成功。不得用其他文件替换缺失输出。已持久的回执与调用记录不可覆盖。

正常ready后调用`{"action":"consume_receipt","attempt_id":"<id>"}`，桥核对调用、核心Attempt/Request、payload和输出哈希，再通过observe建立Version。底图、输出和调用证据都保持可追溯。看图检查语义要求，交付实际文件与稳定标签；精确像素保护未验证时明确留空/unclear。

## 中断恢复

新进程或新会话用`{"action":"recover"}`。该操作只处理已保存的本地事实，不发起原生调用：

- 仅有dispatch，没有可靠回执：保持unknown/uncertain，保留额度，不重发。
- 已存真实return但无可用输出副本：unknown/submitted，不能遗忘工具已返回，也不能虚构成功。
- 完整receipt已保存、Version尚未接纳：核验后接纳同一输出一次；恢复过程禁止自动改变edit_target，selected保持不变。
- Attempt已保存输出接纳记录：共同核心补索引，接纳键防止重复Version。
- 正式项目状态降级或回执损坏：报告具体阻塞；不猜用户选择或重新生成。

已保存且完整可验证的本地回执可按上述流程恢复；这不保证远程图像服务中断后可查询、取消或恰好执行一次。若原生返回尚未可靠保存，unknown仍需要实际可用证据，不能借离线测试写成已恢复。
