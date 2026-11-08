---
name: before-chopsticks
description: 美食摄影创作与连续修改：在具备原生图像工具的Codex中生图与编辑，管理参考和版本，点评照片、给出实拍指导，并进行基础调色裁切。适用于围绕具体菜品或作品的创作，以及从目标菜图反推制作画面与整套脚本；用户直接发来菜品照片（即使不说话）时也使用。不用于纯食谱问答或菜单营养分析。
---

# 动筷之前

围绕菜品和本轮目标创作或修图，不先问用途、风格或固定构图。

## 工作流程（Workflow）

- 入口、素材、模式及授权次数见 [intake.md](references/core/intake.md)。
- 套图策划与交付见 [storyboard.md](references/core/storyboard.md) §SB-1。
- 用户以菜图或描述为目标成品、要反推过程并策划图片时，先读 [reverse-plan.md](references/core/reverse-plan.md)，再接套图脚本。
- 设计默认、菜型/地域、约束及优先级查 [styling.md](references/core/styling.md)；反馈、选版、换菜及裁切查 [interaction.md](references/core/interaction.md)。
- 光色、空间、食物、保留及点评/实拍按需读 [photography.md](references/core/photography.md)；选择写入请求并核成图，不必每轮全读。

## 回复总则

对用户只说做法和理由，不引用 Skill 文件/路径/链接/原文或以“技能要求”解释。

## 持久化与执行约束

需要跨轮版本、修图或外部执行时读 [state.md](references/core/state.md)，以 `scripts/state.py` 管理作品项目、恢复全部 Attempt；复制素材后验来源/可读性，不手改快照或重建选版。外部执行与恢复按 [codex.md](references/hosts/codex.md)；工具缺失时报缺口，不用旧 CLI/未授权渠道补位。未看图/测像素/核工具事实的对应检查保持 `unclear`。

## 完成闸门与能力状态

生成/编辑须有明确对象、有效要求、必要素材和实际授权；交付实际可访问图、稳定标签、检查及限制，保存编辑目标/未解决状态。探索给方向，点评给有据观察/建议，实拍给布置/试拍调整，均不自动出图。身份/锁定/故事未达成，或缺素材/授权/满足保留的工具/依赖，只阻塞受影响操作。

本地 Codex 开发候选（candidate）；`user-approved` 仅用户接受，`verified` 只限有证据的检查范围，文件可读不证明审美合格，本地调色不证明食物语义未变。一套图通过不证明 Skill 完成/部署/发布；安装版本及发布状态须项目实证。用户说够了即保存结束，不为研究加赠生成。
