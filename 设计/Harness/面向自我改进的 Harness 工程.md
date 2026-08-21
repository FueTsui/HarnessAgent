---
title: "面向自我改进的 Harness 工程"
source: "https://lilianweng.github.io/posts/2026-07-04-harness/"
author:
  - "[[Lilian Weng]]"
published: 2026-07-04
created: 2026-07-28
description: "递归自我改进（RSI）的概念可追溯至 I. J. Good（1965）。他将‘超级智能机器’定义为一种能在所有智力活动中超越人类，并能设计出更好的机器来改进自身的系统。现代 AI 中的这一反馈循环既可能指模型直接重写自身权重，也可以更广义地指模型改进训练流水线和部署系统，从而造就一个在具有经济价值的任务上表现更好的后继模型。"
tags:
  - "clippings"
  - "中文翻译"
---

**递归自我改进（recursive self-improvement，RSI）** 的概念可追溯至 [I. J. Good（1965）](https://philpapers.org/rec/GOOSCT)。他将“超级智能机器”定义为一种能在所有智力活动中超越人类，并能设计出更好的机器来改进自身的系统。[Yudkowsky（2008）](https://www.lesswrong.com/posts/JBadX7rwdcRFzGuju/recursive-self-improvement) 用“递归自我改进”描述一种特定的反馈循环：AI 利用当前的智能，改进产生其智能的认知机制。

在现代 AI 中，这一反馈循环既可能指模型直接重写自身权重，也可以更广义地指模型改进*训练流水线*和*部署系统*，进而造就一个在具有经济价值的任务上表现更好的后继模型。事实表明，前沿实验室中的 AI 研究进展速度已显著加快（[Anthropic](https://www.anthropic.com/institute/recursive-self-improvement)；[OpenAI](https://openai.com/index/how-agents-are-transforming-work/)）。

我特意提到*“部署系统”*，因为连接原始模型与现实世界情境的这一层，似乎与模型的原始智能（即预训练完成后立即进行的评测）同等重要。Claude Code、Codex 等成功的编程智能体产品表明，Harness 是 AI 部署的重要组成部分。**运行框架（Harness）** 是围绕基础模型构建的一套系统：它负责编排执行，决定模型如何思考与规划、如何调用工具并采取行动、如何感知和管理上下文、如何保存产物，以及如何评估结果。

本文将聚焦 Harness 工程相关研究及其对 RSI 的贡献。近期大量关于自动化研究、自我改进智能体和程序进化搜索的工作，都可以围绕这个问题来组织。模型自博弈、合成数据、测试时训练，以及更广泛的持续学习研究，也符合 RSI 的愿景（例如 [Yuan et al. 2024](https://arxiv.org/abs/2401.10020)、[Chen et al. 2024](https://arxiv.org/abs/2401.01335)、[Zhao et al. 2025](https://arxiv.org/abs/2505.03335)、[Choi et al. 2026](https://openreview.net/forum?id=lTbBFAoPSA)），但它们不是本文重点。

## Harness 设计模式

与早期的[智能体框架](https://lilianweng.github.io/posts/2023-06-23-agent/)——“智能体 = LLM + 记忆 + 工具 + 规划 + 行动”——相比，Harness 工程还包括*工作流设计（如循环工程）、评估、权限控制和持久状态管理*。它已不再只是提示词模板，而更接近运行时和软件系统设计：模型如何观察、行动、记忆、自检和改进。

设计应有意保持简单、通用，以便实现泛化；同时可借鉴现有软件工程实践，从预训练知识中获益。操作系统与 Harness 之间也存在很强的类比。与操作系统类似，Harness 应封装复杂逻辑，同时保持接口简洁。与此同时，配置、工具接口及其他协议可能逐渐形成行业标准。

## 模式一：工作流自动化

定义一个可供模型运行、测试和迭代的工作流，是实现自动化的关键设计。Karpathy 的 autoresearch 仓库（[https://github.com/karpathy/autoresearch](https://github.com/karpathy/autoresearch)）清楚展示了如何构建这种工作流。常见工作流遵循面向目标的循环：规划、执行、观察/测试、改进、再次执行，*直到*目标达成。过程中，系统也可能主动向用户询问，以澄清任务要求或执行偏好。

![](https://lilianweng.github.io/posts/2026-07-04-harness/openai-agent-loop.png)

简化的 Codex 智能体循环：智能体调用工具，工具响应影响模型的下一次生成。（图片来源：OpenAI Codex 智能体文章）

工作流图还强调：模型通过“智能体运行时”分析自身轨迹和失败案例，并据此持续迭代，而不是依赖静态提示词模板。

## 模式二：以文件系统作为持久记忆

在长周期智能体系统中，一个反复出现的模式是：以简单方式控制丰富的状态和产物。Harness 不应把整个工作流和全部日志都塞进上下文；相反，它应将持久状态保存在文件中。在长周期智能体运行中，实验日志、代码差异、论文摘要、错误跟踪和历史运行轨迹等产物，往往会远超模型训练时所支持的上下文窗口。

学习如何读取、写入和编辑文件系统（通常通过 `bash` 命令）是 LLM 的基础能力。因此，以文件这种简单形式管理持久记忆，自然会随核心模型能力的提升而受益。

## 模式三：子智能体与后台任务

Harness 可以生成多个子智能体并行执行，并监控后台任务。当主智能体需要探索多个假设、并发运行实验，或委派彼此隔离的子任务而不污染主上下文时，这一点很有用。父智能体需要一个小型进程管理器：启动任务、检查日志、取消失败的运行，并把结果合并回主智能体任务。

关键设计选择是让并行过程显式且可检查。如果子智能体的输出只存在于临时对话上下文中，很快就会过时并被隐藏；如果它们被保存为文件、日志和状态记录，模型便能在中断后恢复，并基于自身执行历史进行推理。

## 案例研究：编程智能体 Harness

Claude Code、Codex、OpenCode 和 Cursor 风格智能体的核心接口已经趋于稳定。它们通常采用如下循环：

![](https://lilianweng.github.io/posts/2026-07-04-harness/coding-harness-loop.png)

借助一组工具，编程智能体能够在给定代码仓库中开发和调试问题，类似于人类开发者使用 IDE。

（以下并非完整清单，仅供演示；感兴趣可阅读[此文](https://github.com/yasasbanukaofficial/claude-code)。）

| 类别 | 工具定义 |
| --- | --- |
| 文件系统 | \- 文件发现：`glob`、`grep`、`ls`　\- 文件读取：`read`、`read_many`　\- 文件修改：`write`（写入全新文件）、`edit`（精确字符串替换）、`multi_edit`、`apply_patch`（应用结构化补丁/差异） |
| Shell 执行 | 运行命令：`bash`、`PowerShell` |
| 输入输出 | `lsp`，以及 `git_status`、`git_diff`、`git_commit` 等 Git 工具 |
| 外部上下文 | MCP 工具、Skills |
| Web 搜索 | `web_search`、`web_fetch`、浏览器工具 |
| 产物 | 读取文档、图片；生成 HTML、图片 |
| 后台进程 | 如 `CronCreate`、`CronDelete`、`CronList` |
| 智能体委派 | 如 `spawn_agent`、`resume_agent`、`wait_agent`、`list_agents`、`close_agent`、`interrupt_agent` 等 |

## Harness 层与核心智能，孰轻孰重？

很难预测未来 RSI 会在多大程度上依赖 Harness 工程，但 RSI 的近期路径不太可能从模型直接重写自身权重开始。我对实际近期路径的预测是：

1. Harness 工程将朝着元方法论方向演进（即改进获得更好答案的机制，而不只是改进答案本身）。Harness 系统本身将成为优化目标，启发式规则更少，通用机制更多。
2. 反过来，成熟的 Harness 将支持模型自我改进循环中的自动化研究；更聪明的模型则能避免 Harness 被过度设计，使整个系统保持可持续。

最终，许多 Harness 改进可能会被*内化*为核心模型行为，但与外部上下文和工具交互的接口仍会保留。我们已经在[提示词工程](https://lilianweng.github.io/posts/2023-03-15-prompt-engineering/)中看到这一模式的温和版本：随着指令微调和模型推理能力提高，手工提示技巧的重要性下降了，但*明确目标、约束、上下文和评估标准的需求并未消失*。

## Harness 优化

Harness 系统中被优化对象的演进大致为：指令[提示词](https://lilianweng.github.io/posts/2023-03-15-prompt-engineering/) → 结构化上下文 → 工作流 → Harness 代码 → 优化器代码。随着模型日益智能、强大，我们逐步转向更复杂的目标和更通用的方法。

## 上下文工程

随着智能体任务周期显著延长，如果只是把所有工具响应和模型生成内容追加到上下文中，其规模很快就会失控。上下文管理层负责为 LLM 构建更有结构、更精炼的上下文，并管理持久状态。长上下文研究无疑会不断进步，但目前长上下文智能与上下文工程有时仍交织在一起。

**智能体上下文工程**（Agentic Context Engineering，ACE；[Zhang et al. 2025](https://arxiv.org/abs/2510.04618)）把上下文视为一份不断演进的操作手册，而不是一段越来越长的提示词。它通过三个组件维护由要点组成的上下文手册，每个要点都有标识符和描述：

1. *生成器（Generator）*：参考要点生成任务轨迹。
2. *反思器（Reflector）*：从成功和失败的轨迹中提炼洞见。
3. *策展器（Curator）*：以增量、逐条的方式更新结构化上下文。

![](https://lilianweng.github.io/posts/2026-07-04-harness/ace.png)

智能体上下文工程（ACE）框架。（图片来源：Zhang et al. 2025）

为防止迭代重写过程中的上下文坍缩和简短偏差，ACE 的一个关键设计是：策展器不重写完整的提示词块，而是输出一组采用“标识符—描述”形式的结构化条目，再由确定性逻辑将它们合并进结构化上下文日志。系统会定期精炼条目并去重。

ACE 从运行轨迹中学习洞见，使我们向自主管理记忆迈进，但其更新规则和整体工作流仍是手工设计的。为迈向更具自我改进能力的循环，**元上下文工程**（Meta Context Engineering，MCE；[Ye et al. 2026](https://arxiv.org/abs/2601.21557)）将机制（如何管理上下文）与产物内容（上下文中有什么）分离：在元优化层演化技能，在基础层优化上下文。

一个 MCE 技能 $s \in \mathcal{S}$ 定义上下文函数 $c_{s} = \left(\rho_{s} , F_{s}\right)$，并将输入 $x$ 映射为上下文 $c = F_{s} \left(x ; \rho_{s}\right)$，其中：

- $\rho_{s} = \left\{\rho_{1} , \ldots , \rho_{m}\right\}$ 是静态组件（提示词、知识库、代码库）。
- $F_{s} = \left\{F_{1} , \ldots , F_{k}\right\}$ 是动态算子（搜索、选择、过滤、格式化）。

双层优化的内层目标，是在给定技能 $s$ 时找出训练数据上的最佳上下文 $c_{s}^{*}$；外层则寻找能在验证集上取得最佳表现的技能：

$$
\text{Inner}:\text{ } c_{s}^{*} = arg \underset{c_{s}}{max} J_{\text{train}} \left(c_{s} ; s\right) \text{Outer}:\text{ } s^{*} = arg \underset{s \in \mathcal{S}}{max} J_{\text{val}} \left(c_{s}^{*}\right)
$$

技能数据库跟踪以往技能、上下文函数和评估指标的历史：$\mathcal{H}_{k - 1} = \left\{\right. \left(s_{i} , c_{i} , J_{i}^{\text{train}} , J_{i}^{\text{val}}\right) \left.\right\}_{i = 1}^{k - 1}$。元层智能体基于以往技能执行智能体式[交叉](https://en.wikipedia.org/wiki/Crossover_\(evolutionary_algorithm\))，针对任务 $\tau$ 创建新技能：$s_{k} = \text{crossover} \left(\tau , \mathcal{H}_{k - 1}\right)$。

随后，基础层上下文工程师执行技能 $s_{k}$，在当前技能指导下，从运行反馈 $\mathcal{R}_{k}$ 中学习上下文函数：$c_{k} = \text{engineer} \left(\tau , s_{k} ; c_{k - 1}^{*} , \mathcal{R}_{k}\right)$。

![](https://lilianweng.github.io/posts/2026-07-04-harness/mce.png)

元上下文工程（MCE）框架：元层技能演化搜索上下文管理机制，基础层则优化任务上下文。（图片来源：Ye et al. 2026）

MCE 不像 ACE 那样强制采用启发式的上下文结构规则。它使用*自由形式的技能*保存任务最重要的知识，并让技能及其条件化上下文共同迭代演化。在实现上，上下文函数 $c$ 被实例化为专用目录中的一组文件，既包括静态组件（`skill.md`），也包括动态组件（上下文和数据运行轨迹）。元层和基础层优化都在配有标准工具集的智能体编程环境中执行：

$$
\mathcal{T} = \left\{\mathtt{Read} , \mathtt{Write} , \mathtt{Edit} , \mathtt{Bash} , \mathtt{Glob} , \mathtt{Grep} , \mathtt{TodoWrite}\right\}
$$

**Meta-Harness**（[Lee et al. 2026](https://arxiv.org/abs/2603.28052)）又深入了一层：其优化对象是决定并优化哪些信息应被保存、检索和呈现给模型的*代码*。名称中的“Meta-”意味着它是一个用于优化 Harness 的 Harness。

![](https://lilianweng.github.io/posts/2026-07-04-harness/meta-harness-outer-loop.png)

Meta-Harness 外循环优化算法。（图片来源：Lee et al. 2026）

负责提出新 Harness 的提议器本身就是一个编程智能体，最终输出为帕累托前沿上的一组 Harness 候选方案。

- 整个执行历史都可通过文件系统访问，因此编程智能体使用 `grep`、`cat` 等命令读取历史，而不是把所有内容硬塞进单一提示上下文。
- 每个候选 Harness 都是文件系统中的一个目录，包含自身源代码、分数、运行轨迹和状态更新。
- Meta-Harness 循环不断创建新 Harness，只有合格者会被保留。

![](https://lilianweng.github.io/posts/2026-07-04-harness/meta-harness.png)

Meta-Harness 在少量迭代的文本分类任务（左）和 TerminalBench-2（右）上的表现。请注意，TerminalBench-2 实验中的搜索从 Terminus-KIRA 和 Terminus-2 这两个很强的 Harness 开始。（图片来源：Lee et al. 2026）

尽管如此，重要结论已经很清楚：一旦 Harness 设计成为可执行的搜索空间，强大的编程智能体就能利用与人类工程师相同的设计空间。

## 工作流设计

Harness 工程中的工作流可以由领域专家手工设计。以自动化研究为例，人们已经提出并测试了多种框架。**AI Scientist** 系统（[Lu et al. 2026](https://www.nature.com/articles/s41586-026-10265-5)）构建了一条流水线，用于提出研究想法、编写代码、运行实验、分析结果、撰写论文和执行同行评审。[Meng et al.（2026）](https://arxiv.org/abs/2605.26340)则在 **ScientistOne** 中把可验证性作为核心设计约束：每项主张（引文、数值、方法或结论）都必须追溯到证据来源，并接受证据链检查。

![](https://lilianweng.github.io/posts/2026-07-04-harness/ai-scientist.png)

AI Scientist 用于创意生成、实验、论文写作和评审的流水线。（图片来源：Lu et al. 2026）

**Autodata** 智能体（[Kulikov et al. 2026](https://arxiv.org/abs/2606.25996)）被设计为一名数据科学家，用于生成训练和评估数据。主智能体管理四种角色：提出问题的*挑战者*、*弱求解器*、*强求解器*以及*验证器/裁判*。其目标是合成难度“恰到好处”的数据，即强求解器能够成功，而弱求解器会失败。

在 Autodata 中，挑战者提示词根据求解器和验证器的反馈迭代更新。其局限在于：合成任务用于微调弱求解器，却不用于强求解器；如果循环无法迭代改进强模型，它就更像是在生成的提示分布上进行间接蒸馏，RSI 的意味较弱。

![](https://lilianweng.github.io/posts/2026-07-04-harness/autodata.png)

Autodata 围绕挑战者、求解器和验证器角色生成合成训练与评估数据的智能体工作流。（图片来源：Kulikov et al. 2026）

工作流的设计空间*极其庞大*。我们自然可以把工作流设计视为搜索问题，因此应能通过算法寻找优秀方案，而不只是手工打造。沿着这一方向，**智能体系统自动化设计**（Automated Design of Agentic Systems，ADAS；[Hu et al. 2025](https://arxiv.org/abs/2408.08435)）把智能体设计本身表述为一个优化问题，即让元智能体提出新的智能体工作流设计。

1. 用思维链（CoT）、自我精炼等简单智能体初始化一个智能体工作流档案库。
2. 让元智能体参考档案库中的既有方案，以*代码*编写新智能体。
	- 元智能体先生成新工作流的高层描述，再用代码实现。
	- 草稿程序随后由元智能体执行两轮自我精炼（即先让模型提供反馈，再让同一模型依据反馈改进先前输出；[Madaan et al. 2023](https://arxiv.org/abs/2303.17651)），以检查其新颖性。
3. 评估每个新候选方案，把成功者放回档案库。
4. 重复第 2～3 步，直到达到最大迭代次数。

![](https://lilianweng.github.io/posts/2026-07-04-harness/adas.png)

智能体系统自动化设计（ADAS）示意图。（图片来源：Hu et al. 2025）

**AFlow**（[Zhang et al. 2025](https://arxiv.org/abs/2410.10762)）把智能体工作流表示成图：节点代表调用 LLM 的动作，边则以代码实现逻辑操作。工作流优化依赖 [MCTS](https://en.wikipedia.org/wiki/Monte_Carlo_tree_search)（蒙特卡洛树搜索）：

1. 使用模板在树中初始化起始工作流 $W_{0}$。
2. 以评分与均匀探索的柔性混合方式选择工作流节点。
3. 让 LLM 根据该节点的评估表现生成修改后的工作流，从而扩展节点。
4. 执行并评估新工作流。
5. 如果新工作流在 $N$ 轮预算内有所改进，就将其加入树中。
6. 重复第 2～5 步，当前 $k$ 名的平均分趋于稳定或预算耗尽时停止。

![](https://lilianweng.github.io/posts/2026-07-04-harness/aflow.png)

AFlow 在工作流候选树上的优化过程。（图片来源：Zhang et al. 2025）

AFlow 在问答、编程和数学任务上的实验显示，相较于手工工作流和 ADAS，它取得了不错的提升。

![](https://lilianweng.github.io/posts/2026-07-04-harness/aflow-exp.png)

AFlow 与手工方法及 ADAS 的实验对比。（图片来源：Zhang et al. 2025）

## 自我改进的 Harness

无论上下文工程还是工作流设计，都只是 Harness 的一部分。我们需要搜索整个设计空间，同时优化上下文管理逻辑、工作流、权限及其他诸多 Harness 组件。正如 Meta-Harness、ADAS 和 AFlow 等工作所展示的，**✨代码✨** 是定义程序和系统的**通用语言**。简单来说，Harness 就是规定提示词、工具调用、子智能体、控制流、记忆和工作流逻辑如何协作的代码。如果 LLM 能优化用于执行智能体的代码，它就能进入一个比手写提示词*大得多的设计空间*。

**自学优化器**（Self-Taught Optimizer，STOP；[Zelikman et al. 2023](https://arxiv.org/abs/2310.02304)）是递归改进脚手架的早期案例之一。在 $t = 0$ 时，种子改进器 $I_{0}$ 接收初始解 $s$、效用函数 $u$ 和黑盒语言模型 $M$，返回改进后的解 $s^{'}$，即 $s^{'} = I \left(u , s ; M\right)$。STOP 的目标不是直接改进 $s$，而是*改进改进器 $I$ 本身*。

首先，将元效用定义为给定改进器函数 $I$ 在一组下游任务 $\mathcal{D}$ 上的平均效用：

$$
\hat{u} \left(I\right) \triangleq \frac{1}{\left|\mathcal{D}\right|} \mathbb{E}_{\left(u , s\right) \sim \mathcal{D}} \left[u \left(I \left(u , s ; M\right)\right)\right]
$$

由于改进改进器函数本身也是优化问题，我们可以通过自我改进更新，根据元效用衡量的 $I_{t - 1}$ 表现递归得到新版 $I_t$：

$$
I_{t} = I_{t - 1} \left(\hat{u} , I_{t - 1} ; M\right)
$$

![](https://lilianweng.github.io/posts/2026-07-04-harness/STOP-algo.png)

自学优化器（STOP）算法。（图片来源：Zelikman et al. 2023）

实验中，改进后的改进器发现了多种策略，包括遗传算法、分解并改进局部、多臂提示词老虎机、模拟退火、改变温度，以及束搜索/树搜索。这类似于把 Harness 工作流表示为可优化的对象。

![](https://lilianweng.github.io/posts/2026-07-04-harness/STOP-patterns.png)

STOP 发现的自我改进策略示例。（图片来源：Zelikman et al. 2023）

Zelikman et al.（2023）给出了一个值得*警惕*的结果：使用 GPT-4 时，STOP 能在迭代中提高下游任务的平均表现；但使用 GPT-3.5、Mixtral 等较弱模型时，表现反而下降。仅有递归结构并不足够，基础模型必须*足够有能力*去改进这种机制。这意味着，Harness 改进虽能让模型得到更好的部署，但智能仍是核心。

[Lin et al.（2026）](https://arxiv.org/abs/2605.30621)更细致地研究了 Harness 演化对模型能力的依赖。他们拆分出两个维度：（1）*Harness 更新能力*，即生成有用 Harness 修改的能力；（2）*Harness 受益能力*，即利用更新后的 Harness 更好地解决任务的能力。有趣的是，实验发现，从 Qwen3.5-9B 到 Claude Opus 4.6，不同规模和核心智能水平的模型表现出相近的 Harness 更新能力；9B 的 Harness 提议器/演化器甚至能编写出与 Opus 在程序结构上同构的技能。要充分利用 Harness，模型必须能正确、及时地调用技能/工具，并擅长长周期指令遵循。

![](https://lilianweng.github.io/posts/2026-07-04-harness/harness-update.png)

主要结果：（A）从 Qwen2-32B 到 Opus 4.6，各模型的 Harness 更新能力基本持平；（B）Harness 受益能力并非单调变化，中档模型获益最大。（图片来源：Lin et al. 2026）

较新的工作 **Self-Harness**（[Zhang et al. 2026](https://arxiv.org/abs/2606.09498)）依赖 LLM 智能体通过“提出—评估—接受”循环改进自身 Harness。

![](https://lilianweng.github.io/posts/2026-07-04-harness/self-harness.png)

Self-Harness 通过“弱点挖掘—有界 Harness 提案—验证”循环更新 Harness。（图片来源：Zhang et al. 2026）

Self-Harness 循环包括三个阶段：

1. *弱点挖掘*：把失败聚类成以验证器结果为依据的失败模式。
	- 使用当前 Harness $h_t$ 执行任务评估，并收集执行轨迹用于分析。
	- 两次运行表面上可能在错误日志中呈现相同的验证器结果（如超时或缺少产物），实际因果机制却不同。因此，需要内容丰富的失败记录，包括验证器层面的最终原因、相关智能体行为的因果状态，以及轨迹暴露出的抽象智能体机制，从而揭示根因。
2. *Harness 提案*：依据挖掘出的失败模式，提出范围受限的 Harness 修改。
	- 在 $h_t$ 下调用同一模型作为提议器。
	- 为模型提供有界的提案上下文：（1）当前 Harness 的可编辑面；（2）评估系统给出的、以验证器为依据的失败模式；（3）应予保留的成功行为记录；（4）以往修改尝试的摘要。
	- Harness 修改应优先针对反复出现且可以解决的错误模式（而非特定任务本身的难度），并尽量通过小范围修改解决。
	- Harness 修改候选项应彼此不同且具有多样性。
3. *提案验证*：验证并合并合格修改，创建新 Harness $h_{t + 1}$。
	- 在保留的内部数据 $D_{\text{in}}$（测试弱点是否已解决）和留出数据 $D_{\text{out}}$（检查是否引入其他未知问题）上，通过回归测试评估候选修改。
	- 只有在内部和留出数据上均无回归的候选项才会被接受。
	- 接受的候选项将被合并以更新 Harness 至 $h_{t + 1}$；拒绝的候选项仅记录日志，不更改当前 Harness。

在 Terminal-Bench-2 上运行 `MiniMax M2.5`、`Qwen3.5-35B-A3B` 和 `GLM-5` 时，Self-Harness 能针对不同基础模型的不同弱点学习模型专属的 Harness 指令，并提高留出集通过率。

Self-Harness 类工作也引起了我的担忧：如果允许程序编辑操作系统，抽象边界就会被打破。必须妥善设计可编辑面，并将权限控制和安全层置于循环之外。[奖励作弊](https://lilianweng.github.io/posts/2024-11-28-reward-hacking/)相关的所有挑战依然存在。

**智能体 Harness 工程**（Agentic Harness Engineering，AHE；[Lin et al. 2026](https://arxiv.org/abs/2604.25850)）认为 Harness 演化的瓶颈在于**可观测性**：当一次运行失败时，我们需要知道哪个组件应负责任，而且每项修改都应有证据支撑。

该框架围绕三个可观测性支柱构建闭环：

1. *组件可观测性*：每个可编辑 Harness 组件都在文件系统中有对应表示，使行动空间显式且可追踪。
	- Harness 包含七个组件：系统提示词、工具描述、工具实现、中间件、技能、子智能体配置和长期记忆。
	- 每种失败模式都映射到一个组件，使修改更有针对性。
2. *经验可观测性*：分析并汇总大量原始轨迹，形成证据和失败模式的层级结构。
	- 每个 Harness 生成 $k$ 条轨迹。
	- 使用“智能体调试器”分析每条单独存入文件的轨迹，并针对任务的失败或成功根因生成分析报告。
	- 所有逐任务报告汇总为基准测试概览，供下一步使用；必要时仍可访问原始轨迹。这种分层访问结构更节省 token。
3. *决策可观测性*：每项修改都附带对下一轮结果的预测，以供验证。
	- “演化智能体”读取仓库，决定修改哪个组件，然后给出修改及其理由。
	- 每项修改都是文件级、可证伪的主张，可在下一轮验证，并受两项约束：
	- （1）修改仅应用于 Harness 工作区。运行目录、跟踪器、验证器和 LLM 配置均为只读，从而阻止关闭验证器、更换模型、增加推理预算等一系列奖励作弊行为，确保每项已记录的增益都可归因于 Harness 修改。
	- （2）修改必须由证据驱动，并附一条说明：失败证据名称、推断的根因、针对性修复，以及包含预期修复和潜在回归风险的影响预测。

在 Terminal-Bench-2 上，除 Hard 难度层级以及少数自演化基线（ACE、TF-GRPO）外，AHE 的表现优于人工设计的 Harness（OpenCode、Terminus-2、Codex）。同一个冻结后、不再演化的 Harness 还能迁移到 SWE-bench Verified，说明演化后的 Harness 能把工程经验编码进 Harness 组件，而非只针对基准测试进行优化。

## 进化搜索

进化搜索是一种受自然选择启发的优化方法（参见我以前关于[进化算法](https://lilianweng.github.io/posts/2019-09-05-evolution-strategies/)的文章）。它通过对解种群进行变异，并只保留群体中“适应度”较高的个体来推进演化。当（1）搜索空间巨大或形状怪异；（2）难以直接用梯度优化、却容易评估解时，进化搜索尤其有用。Harness 搜索似乎正适合这一方法。

以往研究已将进化搜索用于提示词工程。**Promptbreeder**（[Fernando et al. 2023](https://arxiv.org/abs/2309.16797)）通过丰富的变异操作优化特定任务的提示词；有趣的是，变异提示词（即指示 LLM 如何改变任务提示词的指令）本身也通过进化得到改进。**GEPA**（[Agrawal et al. 2025](https://arxiv.org/abs/2507.19457)）将基于[反思](https://lilianweng.github.io/posts/2023-06-23-agent/#self-reflection)的提示方法与进化搜索结合，利用对反复试错轨迹的自然语言反思提出提示词更新。

[Novikov et al.（2025）](https://arxiv.org/abs/2506.13131)提出 **AlphaEvolve**：一种编程智能体进化搜索系统。它保存候选程序池，并提示冻结的 LLM 生成改进差异。系统反复评估子程序、保留成功者，逐步发现更好的解。

![](https://lilianweng.github.io/posts/2026-07-04-harness/alphaevolve.png)

AlphaEvolve 的工作方式。（图片来源：Novikov et al. 2025）

AlphaEvolve 的设计中有几个重要细节：

- 提示词包含父程序、结果、指令，有时还包括元信息。
- 编程智能体可访问完整仓库，但待改进的代码区域以 `# EVOLVE-BLOCK-START` 和 `# EVOLVE-BLOCK-END` 明确标记。
- 元提示词按照 LLM 的建议与指令和上下文共同演化，方式类似于解程序的演化。

消融实验显示了进化过程、提示词中的上下文、元提示词、全文件演化和更强 LLM 的价值。

![](https://lilianweng.github.io/posts/2026-07-04-harness/alphaevolve-plot.png)

消融实验展示了 AlphaEvolve 多项设计的价值。（图片来源：Novikov et al. 2025）

近期变体中，**ThetaEvolve**（[Wang et al. 2025](https://arxiv.org/abs/2511.23473)）将进化搜索与强化学习、上下文学习相结合；**DemoEvolve**（[Che et al. 2026](https://arxiv.org/abs/2605.24539)）则把人类专家示范加入自运行档案，作为 Harness 层诊断和修改的参考经验。另一方面，**ShinkaEvolve**（[Lange et al. 2025](https://arxiv.org/abs/2509.19349)）引入三个新组件来提高 LLM 采样效率：

- 设计父代采样方法，在性能排名和后代数量之间取得平衡，从而提高探索的样本效率。
- 基于嵌入余弦相似度丢弃与现有种群过于相似的候选项，执行代码新颖性拒绝采样。
- 在元草稿区识别成功方案中的优秀模式，指导后续变异。

与上述聚焦于改进解的方法不同，**达尔文—哥德尔机器**（Darwin Gödel Machine，DGM；[Zhang et al. 2025](https://arxiv.org/abs/2505.22954)）明确以一个可编辑的 Harness 代码仓库为目标，使用基于 LLM 的编程智能体推动其演化。准确地说，该智能体被允许修改自身 Harness。后续的 Hyperagents 工作（[Zhang et al. 2026](https://arxiv.org/abs/2603.19461)）引入元智能体，控制如何修改现有任务智能体以创建新智能体。

1. 从池中的一个编程智能体开始。
2. 每次迭代时，以与性能成正比、与子代数量成反比的概率选择一个父代，对其修改并分支生成新智能体。
3. 选中的父智能体检查自身基准评估日志，随后提出对自身 Harness 代码库的改进，生成新版编程智能体。代码编辑通过两个基础工具实现：（1）bash（参数：`<bash_command>`）；（2）编辑器（参数：`view/create/edit <file_path>`）。
4. 评估新的编程智能体，只有表现足够好的个体才会被放回池中。
5. 重复第 2～4 步，直到满足停止条件。

DGM 是固定模型下的 Harness 演化。实验以 `Claude 3.5 Sonnet` 为基础 LLM，从简单的初始 Harness 配置开始；DGM 发现的智能体在 SWE-bench Verified（20% 提高到 50%）和 Polyglot（14.2% 提高到 30.7%）上达到或超过手工智能体。

当候选解可自动评估、且适应度易于量化时，这类方法效果很好，例如矩阵乘法、GPU 内核优化、算法竞赛和数据中心调度。但在评估缓慢、含糊或主要依赖启发式判断的领域，它们会遇到困难。进化过程的计算效率和有效性也是问题。

## 与模型权重的联合优化

Harness 演化改变的是模型周围的非参数系统。要实现完整的自我改进，也完全可以允许模型同时更新自身权重。权重更新可通过改进模型训练流水线，或在测试时持续学习来实现。持续学习值得未来另写一篇文章讨论。

**SIA**（[Hebbar et al. 2026](https://arxiv.org/abs/2605.27276)）是较早尝试在同一优化循环中结合 Harness 改进和模型参数更新的工作，其设计包含三个组件：

- *元智能体*：提出初始 Harness。
- *特定任务智能体*：执行任务。
- *反馈智能体*：根据近期轨迹决定更新 Harness 还是模型权重。

![](https://lilianweng.github.io/posts/2026-07-04-harness/SIA.png)

SIA 中的反馈智能体决定下一次迭代的类型。（图片来源：Hebbar et al. 2026）

SIA 的实验中有几项混杂选择，使结果难以解释。例如，特定任务智能体远弱于元智能体和反馈智能体使用的模型（`gpt-oss-120b` 对比 `Claude Sonnet 4.6`），基线也太弱，难以与相关方法进行清晰的交叉比较。我认为这一方向很有趣，但目前证据仍属初步。训练稳定性和古德哈特效应等许多挑战依然没有解决。

**Continual Harness**（[Karten et al. 2026](https://arxiv.org/abs/2605.09998)）在长周期游戏环境中进行了实验：更新 Harness，同时通过蒸馏强教师模型对低奖励轨迹的标注，共同学习一个策略模型。

## 未来挑战

AI Scientist 系列工作有力证明，由专家设计的 Harness 能协调自动化研究循环中的很大一部分流程；相关实验以撰写研究论文的形式展开。但生产论文并不等同于科学发现。一个系统可以写出貌似可信的论文，同时仍存在伪造引文、实现偏移或实验结果薄弱等问题。

[Trehan & Chopra（2026）](https://arxiv.org/abs/2601.03315)测试了 LLM 能否仅凭极少量脚手架和基础工具（即 `read_file`、`write_file`、`llm_search`、`list_files`），从研究想法走到论文。每个想法都有专属工作区，智能体可在其中生成和读取文档，将其作为上下文的一部分。他们在三个领域开展实验：世界模型、多智能体强化学习、AI 安全与对齐；每个领域包含 45～50 篇高质量种子文档，用于启发新想法。人类专家只选出四个想法进入完整流程，最终仅有一个被完整执行并形成论文。实验观察到六种反复出现的失败模式：

- *偏向训练数据中的默认做法*：使用旧库、过时命令、标准格式，或采用并非基于实际仓库或数据集的假设。
- *执行压力下的实现偏移*：当实现变得技术复杂时，模型可能转向常见的、更简单的方案，而不是原先提出的方法。
- *记忆和上下文退化*：除非把日志写成持久产物，否则长周期项目会丢失关键细节。
- *过度乐观*：即便实验噪声很大或已经失败，模型仍宣告成功。[Bubeck et al.（2025）](https://arxiv.org/abs/2511.16072)也观察到类似的“p-hacking 与灵光乍现”模式：模型会引入“数字胶带”，在信号仍是噪声时宣布胜利。
- *领域智能不足*：模型缺乏隐性的实践知识，例如预测实现复杂度、判断实验结果是否可信，或了解哪些基线真正重要。
- *科学品味薄弱*：实验或许可执行，却未必回答了正确的问题。

在迈向完整 RSI 的道路上，研究者取得了实质进展，但仍有若干瓶颈。

**1\. 评估器薄弱且模糊。** 许多研究主张没有快速、精确的验证器，很多现实任务也是如此。当评估指标可测量且客观时，当前的自我改进循环表现最好，这与[强化学习的工作方式](https://lilianweng.github.io/posts/2018-02-19-rl-overview/)相似。

研究品味、新颖性和长期科学价值要难衡量得多。例如，研究品味往往混合了问题框定、实验设计，以及判断哪些意外结果值得追踪、哪些失败案例值得重试的能力。

**2\. 上下文和记忆的生命周期。** 随着 AI 智能体更加自主、独立，记忆会不断增长。有效的 Harness 需要管理上下文和记忆，以弥补现有长上下文生成能力的局限，同时尽量提高长周期任务的成功率。人类能维持终生记忆，这里存在一种类比：[上下文工程](#上下文工程)将会、也应该成为智能的核心部分，而不是停留在软件系统层。

**3\. 负面结果。** 研究者受到发表成功结果的激励，因此文献偏向成功。LLM 以海量数据训练（至少目前大部分仍由人类创造，哈哈），而训练数据中成功与失败案例的不平衡，可能使它们不善于判断何时应放弃假设、报告负面结果，甚至承认失败。研究 Harness 应让失败尝试易于保存，因为从失败中学习，是缩小任务搜索空间的最佳方式。

**4\. 多样性坍缩。** 进化循环和强化学习循环倾向于利用已知的高奖励模式。我们需要相应的[机制](https://lilianweng.github.io/posts/2020-06-07-exploration-drl/)，防止种群坍缩成同一方案的各种变体。这对于开放式研究尤其关键，因为最佳路径在当前评估器下最初可能显得更差。

**5\. [奖励作弊](https://lilianweng.github.io/posts/2024-11-28-reward-hacking/)。** 自我改进循环会优化它所接收到的任何信号。如果奖励来自单元测试，智能体可能过拟合测试；如果来自裁判模型，它可能学会针对该裁判的奖励作弊技巧；如果来自基准分数，它可能利用基准中的人为漏洞。

评估器和权限控制很可能应置于 Harness 演化循环之外，并在重要决策点设置留出测试、轨迹审计和人工评审。监督在多大程度上可以扩展和自动化，仍是一个开放研究问题。

**6\. 长期成功。** 外部优化循环作用于单次运行之外、可在训练沙箱中模拟的奖励。

以编程智能体为例，它们已经提高了软件工程的日常生产力，但许多优化目标仍过于短期。它通常能完成眼前任务，却不清楚应如何保护一个由数百或数千名工程师共同维护的仓库的长期健康。标准的、基于沙箱的 RLVR 式训练很少涵盖可维护性、所有权边界、迁移成本、向后兼容性或未来调试负担。

**7\. 人类的角色。** 人类应向更高抽象层移动，而不是被移出循环。这意味着，人类应在正确的时间、正确的抽象层级提供监督；系统设计也应考虑何时、如何设置这样的接触点。

上述许多挑战都需要人类的反馈和引导。归根结底，我们构建技术是为了人类更美好的未来，而不是反过来。

## 引用

请按以下格式引用本文：

> Weng, Lilian. “Harness Engineering for Self-Improvement”. Lil’Log (Jul 2026). https://lilianweng.github.io/posts/2026-07-04-harness/

也可以使用以下 BibTeX：

```
@article{weng2026harness,
  title = {Harness Engineering for Self-Improvement},
   = {Weng, Lilian},
  journal = {lilianweng.github.io},
  year = {2026},
  month = {July},
  url = "https://lilianweng.github.io/posts/2026-07-04-harness/"
}
```

## 附录：一些有用的基准测试

- **[PaperBench](https://arxiv.org/abs/2504.01848)**：从零复现 20 篇 ICML 2024 Spotlight 和 Oral 论文，包括理解论文贡献、开发代码库并成功执行实验。
	- 每项复现任务都拆成更小、可单独评分的任务。
	- 共 8,316 条评分准则，由论文作者共同制定。
	- 当时最佳模型（`Claude 3.5 Sonnet`，约 21%）未能超过机器学习博士。
	- 包括 PaperBench、较轻量的 PaperBench Code-Dev 和 JudgeEval。
- **[CORE-Bench](https://arxiv.org/abs/2409.11363)**：评估已发表研究的计算可复现性。
	- 基于计算机科学、社会科学和医学领域的 90 篇论文，共 270 项任务。
	- 任务要求使用给定代码和数据复现结果。
	- 包含多个难度级别，以及纯语言和视觉—语言任务。
	- 当时报告的最佳智能体（`GPT-4o` 和 `GPT-4o-mini`）在最难任务上准确率仅为 21%。
- **[ScienceAgentBench](https://arxiv.org/abs/2410.05080)**：评估 LLM 智能体进行数据驱动科学发现的能力。
	- 从数学、化学、生物、地理四个学科的 44 篇同行评审论文中提取 102 项任务。
	- 涵盖这些领域的基础数据科学任务：数据处理、模型开发、数据分析和信息可视化。
- **[RE-Bench](https://arxiv.org/abs/2411.15114)**：在真实的机器学习研究工程环境中，将前沿 AI 智能体与人类专家进行比较。
	- 包含七个具有挑战性、开放式的机器学习研究工程环境。
	- 每个环境 =（评分函数、初始解、参考解），使用不超过八块 H100 GPU 即可运行。
	- 示例包括优化内核、运行缩放定律实验、修复嵌入、微调 GPT-2 完成问答等。
	- 包含 61 名不同人类专家所做的 71 次八小时尝试的数据。
	- 人类专家在 82% 的八小时尝试中取得非零分数；24% 的尝试达到或超过强参考解。
	- 在两小时预算下，最佳 AI 智能体得分是人类的四倍；但人类在更长预算下回报更高，并在八小时和 32 小时条件下超过智能体。
- **[MLE-bench](https://arxiv.org/abs/2410.07095)**：在离线 Kaggle 竞赛中评估机器学习工程智能体。
	- 包含 75 项从 Kaggle 精选的机器学习工程竞赛。
	- 测试模型训练、数据集准备、实验运行，以及向评分脚本提交预测的能力。
	- 使用 Kaggle 公开排行榜作为人类基线。
	- 论文中的最佳配置 `o1-preview` 搭配 AIDE 脚手架，在 16.9% 的竞赛中至少达到 Kaggle 铜牌水平。
	- 包含资源缩放和污染分析。
- **[KernelBench](https://arxiv.org/abs/2502.10517)**：评估所生成 GPU 内核的正确性和速度。
	- 包含 250 项 PyTorch 任务，用于评估 LLM 能否编写快速且正确的内核。
	- 评估指标 fast\_p = 正确且快于基线的生成内核所占百分比。

## 参考文献

\[1\] Good, I. J. [“关于第一台超级智能机器的猜想”](https://philpapers.org/rec/GOOSCT)，*Advances in Computers*，6:31–88，1965。

\[2\] Yudkowsky, Eliezer. [“递归自我改进”](https://www.lesswrong.com/posts/JBadX7rwdcRFzGuju/recursive-self-improvement)，LessWrong，2008。

\[3\] Choi, et al. [“用于代码修复的锚定自博弈”](https://openreview.net/forum?id=lTbBFAoPSA)，ICML 2026。

\[4\] Zhao, et al. [“绝对零：零数据强化自博弈推理”](https://arxiv.org/abs/2505.03335)，arXiv:2505.03335，2025。

\[5\] Yuan, et al. [“自奖励语言模型”](https://arxiv.org/abs/2401.10020)，arXiv:2401.10020，2024。

\[6\] Chen, et al. [“自博弈微调将弱语言模型转化为强语言模型”](https://arxiv.org/abs/2401.01335)，ICML 2024。

\[7\] Zhang, et al. [“智能体上下文工程：为自我改进语言模型演化上下文”](https://arxiv.org/abs/2510.04618)，ICLR 2026。

\[8\] Ye, et al. [“通过智能体技能演化实现元上下文工程”](https://arxiv.org/abs/2601.21557)，arXiv:2601.21557，2026。

\[9\] Lee, et al. [“Meta-Harness：模型 Harness 的端到端优化”](https://arxiv.org/abs/2603.28052)，arXiv:2603.28052，2026。

\[10\] Lu, et al. [“迈向 AI 研究的端到端自动化”](https://www.nature.com/articles/s41586-026-10265-5)，*Nature*，651:914–919，2026。

\[11\] Meng, et al. [“ScientistOne：通过证据链迈向人类水平的自主研究”](https://arxiv.org/abs/2605.26340)，arXiv:2605.26340，2026。

\[12\] Kulikov, et al. [“Autodata：创建高质量合成数据的智能体数据科学家”](https://arxiv.org/abs/2606.25996)，arXiv:2606.25996，2026。

\[13\] Hu, Lu, and Clune. [“智能体系统自动化设计”](https://arxiv.org/abs/2408.08435)，ICLR 2025。

\[14\] Madaan, et al. [“Self-Refine：利用自反馈进行迭代精炼”](https://arxiv.org/abs/2303.17651)，NeurIPS 2023。

\[15\] Zhang, et al. [“AFlow：智能体工作流生成自动化”](https://arxiv.org/abs/2410.10762)，ICLR 2025。

\[16\] Zelikman, et al. [“自学优化器（STOP）：递归自我改进的代码生成”](https://arxiv.org/abs/2310.02304)，COLM 2024。

\[17\] Zhang, et al. [“Self-Harness：能够自我改进的 Harness”](https://arxiv.org/abs/2606.09498)，arXiv:2606.09498，2026。

\[18\] Fernando, et al. [“Promptbreeder：通过提示词演化实现自指式自我改进”](https://arxiv.org/abs/2309.16797)，arXiv:2309.16797，2023。

\[19\] Agrawal, A. et al. [“GEPA：反思式提示词演化能够超越强化学习”](https://arxiv.org/abs/2507.19457)，arXiv:2507.19457，2025。

\[20\] Novikov, et al. [“AlphaEvolve：用于科学和算法发现的编程智能体”](https://arxiv.org/abs/2506.13131)，arXiv:2506.13131，2025。

\[21\] Lange, Imajuku, and Cetin. [“ShinkaEvolve：迈向开放式、样本高效的程序演化”](https://arxiv.org/abs/2509.19349)，arXiv:2509.19349，2025。

\[22\] Wang, et al. [“ThetaEvolve：开放问题上的测试时学习”](https://arxiv.org/abs/2511.23473)，arXiv:2511.23473，2025。

\[23\] Zhang, et al. [“达尔文—哥德尔机器：自我改进智能体的开放式演化”](https://arxiv.org/abs/2505.22954)，arXiv:2505.22954，2025。

\[24\] Zhang, et al. [“Hyperagents”](https://arxiv.org/abs/2603.19461)，arXiv:2603.19461，2026。

\[25\] Yuksekgonul, et al. [“在测试时学习发现”](https://arxiv.org/abs/2601.16175)，arXiv:2601.16175，2026。

\[26\] Riaz, et al. [“测试时发现中的认知不确定性”](https://arxiv.org/abs/2605.11328)，arXiv:2605.11328，2026。

\[27\] Hebbar, et al. [“SIA：通过 Harness 与权重更新实现自我改进 AI”](https://arxiv.org/abs/2605.27276)，arXiv:2605.27276，2026。

\[28\] Trehan and Chopra. [“为何 LLM 还不是科学家：四次自主研究尝试的经验”](https://arxiv.org/abs/2601.03315)，arXiv:2601.03315，2026。

\[29\] Bubeck, et al. [“使用 GPT-5 开展早期科学加速实验”](https://arxiv.org/abs/2511.16072)，arXiv:2511.16072，2025。

\[30\] Starace, et al. [“PaperBench：评估 AI 复现 AI 研究的能力”](https://arxiv.org/abs/2504.01848)，ICML 2025。

\[31\] Wijk, et al. [“RE-Bench：将前沿 AI 研发智能体与人类专家进行比较评估”](https://arxiv.org/abs/2411.15114)，ICML 2025。

\[32\] Chan, et al. [“MLE-bench：在机器学习工程任务上评估机器学习智能体”](https://arxiv.org/abs/2410.07095)，arXiv:2410.07095，2024。

\[33\] Chen, et al. [“ScienceAgentBench：面向数据驱动科学发现的语言智能体严格评估”](https://arxiv.org/abs/2410.05080)，ICLR 2025。

\[34\] Siegel, et al. [“CORE-Bench：通过计算可复现性智能体基准提升已发表研究的可信度”](https://arxiv.org/abs/2409.11363)，TMLR 2024。

\[35\] Ouyang, et al. [“KernelBench：LLM 能否编写高效的 GPU 内核？”](https://arxiv.org/abs/2502.10517)，arXiv:2502.10517，2025。

\[36\] Lin, et al. [“Harness 更新不等于 Harness 受益：解耦自演化 LLM 智能体的演化能力”](https://arxiv.org/abs/2605.30621)，arXiv:2605.30621，2026。

\[37\] Lin, et al. [“智能体 Harness 工程：由可观测性驱动的编程智能体 Harness 自动演化”](https://arxiv.org/abs/2604.25850)，arXiv:2604.25850，2026。

\[38\] Karten, et al. [“Continual Harness：自我改进基础智能体的在线适应”](https://arxiv.org/abs/2605.09998)，arXiv:2605.09998，2026。

\[39\] Che, et al. [“DemoEvolve：以示范克服智能体 Harness 演化中的稀疏反馈”](https://arxiv.org/abs/2605.24539)，arXiv:2605.24539，2026。
