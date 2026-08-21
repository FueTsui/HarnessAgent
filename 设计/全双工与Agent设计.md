---
title: "全双工与Agent设计"
source: "https://chatgpt.com/c/6a806ec5-de74-83e8-8cc8-a5dc80cd8673"
author:
published:
created: 2026-08-20
description: "ChatGPT conversation with 6 messages"
tags:
  - "clippings"
---
全双工模式可以不可以放在agent里面

---

可以，而且从 Agent 架构角度看， **全双工非常适合做成 Agent 的一种运行模式** 。但我不建议把“全双工”直接等同于 Agent，而是把它作为 **Agent Runtime 的实时交互层** 。

Moshi 已经证明了一个关键点：用户语音流和模型输出语音流可以作为并行流同时建模，从而支持“边听边说”、插话、打断、backchannel 等行为。 [^1] 更进一步，2026 年的 MoshiRAG 已经采用了非常接近 Agent 的结构： **全双工前端持续对话，后端异步执行检索/推理，二者并行运行** ；检索完成后再把结果注入正在进行的对话。 [^2]

### 我更推荐你把 Agent 设计成这样

```
┌─────────────────────┐
                        │       用户 User      │
                        └──────────┬──────────┘
                                   │
                     Audio / Text / Event
                                   │
                                   ▼
┌───────────────────────────────────────────────────┐
│           Full-Duplex Interaction Layer           │
│                                                   │
│   Listen Stream              Speak Stream         │
│       ↓                          ↑                │
│   Streaming ASR            Streaming Voice        │
│       │                          │                │
│       ├──── Barge-in / Interrupt ─┤               │
└───────┼──────────────────────────┼────────────────┘
        │                          ▲
        ▼                          │
┌───────────────────────────────────────────────────┐
│                  Agent Runtime                    │
│                                                   │
│  Intent → Context → Planner → Executor → Verifier │
│                         │                         │
│              ┌──────────┼──────────┐              │
│              ▼          ▼          ▼              │
│             RAG        MCP       Skills           │
│              │          │          │              │
│              └──────────┼──────────┘              │
│                         │                         │
│                    Tool Results                   │
└─────────────────────────┼─────────────────────────┘
                          │
                          ▼
                  Conversation State
                          │
                          └────→ 实时反馈给用户
```

这里最重要的一点是：

> **Agent 不应该因为正在执行任务，就失去“听”的能力。**

传统 Agent 往往是：

```
用户输入
 ↓
Thinking
 ↓
调用 Tool
 ↓
等待 Tool
 ↓
生成回答
 ↓
重新等待用户
```

这是典型的 **半双工/Turn-based Agent** 。

你可以改成：

```
用户输入 ───────────────→ Agent
   ↑                       │
   │                       ├── Planner
   │                       ├── Tool A
   │                       ├── Tool B
   │                       ├── RAG
   │                       └── Validator
   │
   └──── Agent 输出 ←───────┘
```

输入和输出通道 **永远同时开放** 。

---

## 例如一个真正的 Full-Duplex Agent

你对它说：

> “帮我分析一下今天A股科技股为什么下跌。”

Agent 开始执行：

```
00.0s 用户开始说话
00.3s Streaming ASR 开始理解

01.2s Agent：
      “可以，我先看一下今天科技板块的资金流向……”

01.4s 后台：
      ├─ 搜索市场行情
      ├─ 获取北向/机构资金
      ├─ 获取板块资金
      ├─ 新闻检索
      └─ 开始分析

03.0s Agent继续：
      “目前初步看不是单一因素……”

03.5s 用户突然插话：
      “等等，我主要想看工业富联和科大讯飞。”

03.6s Interrupt Event
      ↓
取消原来部分任务
      ↓
Planner Re-plan

03.8s Agent：
      “明白，那我缩小到工业富联和科大讯飞。”

后台立即：

      ├─ cancel broad_market_analysis
      ├─ search 601138
      ├─ search 002230
      └─ compare capital flow
```

这时候才是真正意义上的：

**Agent + Full Duplex。**

而不只是“语音聊天”。

---

## 对你一直研究的 Harness / Agent 架构来说，可以进一步做成 5 条并行 Loop

```
┌──────────────────────────────────────────────┐
│              Agent Runtime                  │
│                                              │
│  Loop 1   Perception Loop                   │
│           持续监听用户 / 环境                │
│                                              │
│  Loop 2   Reasoning Loop                    │
│           持续判断 / Planning                │
│                                              │
│  Loop 3   Execution Loop                    │
│           MCP / Skill / Tool / Subagent      │
│                                              │
│  Loop 4   Interaction Loop                  │
│           实时向用户反馈                     │
│                                              │
│  Loop 5   Verification Loop                 │
│           Validator / Watchdog / Audit       │
└──────────────────────────────────────────────┘
```

它们不是：

```
1 → 2 → 3 → 4 → 5
```

而是：

```
Perception
          ↕
Reasoning ↔ Execution
    ↕         ↕
Interaction ↔ Verification
```

也就是一个 **并发事件系统** 。

这其实和你之前研究的 Harness Engineering 非常契合：Harness 不再只是管理模型“下一步干什么”，而是管理多个持续运行的 Loop。

---

## 实现时最关键的是 Event Bus

不要让模块彼此直接死调用：

```python
speech -> planner -> tool -> speech
```

更推荐：

```
Event Bus
                      │
      ┌───────────────┼───────────────┐
      ↓               ↓               ↓
  Listener         Planner        Executor
      ↓               ↓               ↓
user.speech      plan.created     tool.result
user.stop        plan.updated     tool.error
user.interrupt   plan.cancelled   tool.progress
      │               │               │
      └───────────────┼───────────────┘
                      ↓
                 Response Agent
```

例如事件：

```json
{
  "type": "user.interrupt",
  "timestamp": 1723730000,
  "priority": 100,
  "session_id": "xxx"
}
```

Planner 收到：

```
user.interrupt
```

马上：

```
暂停输出
↓
判断用户新意图
↓
cancel / continue / modify 当前任务
↓
重新规划
```

这比简单 WebSocket 双向传输要高级得多。

---

## 还有一个特别重要的设计：不要让语音模型直接承担所有 Agent 推理

MoshiRAG 的设计其实已经给出了一个很好的方向：

```
Full Duplex Model
   实时、小、快
        │
        │ Async
        ▼
 Powerful Backend
大模型 / RAG / Tools
```

论文中的架构就是全双工前端继续交互，同时后台异步检索外部知识，而且作者明确把这种结构进一步延伸到 tool-use Agent 方向。 [^2]

所以我更推荐：

```
Fast Agent
────────────────
负责：
听
说
插话判断
简单意图
backchannel
任务状态反馈

        ↕

Deep Agent
────────────────
负责：
Reasoning
Planning
RAG
MCP
Skill
Code
Search
Subagent
Validator
```

而不是：

```
一个 100B 模型
同时负责
语音 + 推理 + MCP + RAG + Agent + TTS
```

后者成本、延迟、任务阻塞都会比较严重。

---

## 最终可以形成你之前那套 Agent 架构的升级版

我会把它定义成：

**Full-Duplex Event-Driven Agent Runtime**

```
Human
  ↕
Realtime Duplex Interface
  ↕
────────────────────────────
        Event Bus
────────────────────────────
  ↕        ↕        ↕
Planner  Memory   Context
  ↕        ↕        ↕
────────────────────────────
       Agent Harness
────────────────────────────
  ↓       ↓       ↓       ↓
Skill    MCP     RAG    SubAgent
  ↓       ↓       ↓       ↓
────────────────────────────
      Execution Runtime
────────────────────────────
             ↕
     Validator / Watchdog
             ↕
────────────────────────────
        Duplex Output
────────────────────────────
             ↕
            Human
```

所以你的问题答案不只是\*\*“能不能”\*\*：

**能，而且 Full-Duplex + Agent + Harness + Event Bus 很可能比传统 ReAct 式串行 Agent 更适合下一代实时智能体。**

尤其值得把“全双工”从单纯的 **语音能力** 进一步抽象成 **Agent 调度能力** ：用户可以随时打断、任务可以并行执行、工具结果可以异步返回、Agent 可以边工作边汇报、Planner 可以动态重规划、Validator 可以后台持续审计。这会比单纯给 Agent 接一个全双工语音模型更有价值。 [^2]

如果按照你之前设计的 **Harness + Graph + Loop + MCP + Skill + Validator + Watchdog** 那套体系继续演进，我认为 **Full-Duplex Runtime / Event Bus** 正好可以成为它最外层的实时运行框架。

---

websocket和全双工的区别

---

WebSocket 和“全双工”不是同一层级的概念。

**一句话：WebSocket 是一种通信协议/技术实现；全双工是一种通信能力或通信模式。**

可以把它理解成：

```
全双工 = “双方能不能同时说话”
WebSocket = “双方通过什么线路和协议说话”
```

### 1\. 全双工是什么

Full-Duplex 描述的是：

> A 和 B 可以在同一时刻同时发送和接收数据。

例如：

```
客户端 ─────→ 服务端
客户端 ←───── 服务端
```

这两个方向可以同时发生。

典型例子是电话：

- 你可以说话
- 对方也可以同时说话
- 双方不用轮流占用信道

这就是全双工。

相对的是：

```
单工 Simplex
A ───→ B

半双工 Half-Duplex
A ───→ B
A ←─── B
但不能同时
```

对讲机就是典型半双工。

---

### 2\. WebSocket 是什么

WebSocket 是一个具体的应用层通信协议。

最常见结构：

```
Browser
   │
   │ HTTP Upgrade
   ▼
Server

建立 WebSocket 后：

Browser ⇄ Server
```

连接建立之后，客户端和服务端都可以主动发消息。

所以：

> **WebSocket 本身支持全双工通信。**

例如服务端不需要等客户端请求：

```javascript
socket.send("任务完成")
```

客户端也可以随时：

```javascript
socket.send("停止任务")
```

双方可以同时发送。

---

## 3\. 最大区别

| 对比 | WebSocket | 全双工 |
| --- | --- | --- |
| 本质 | 协议 | 通信模式 |
| 属于哪层 | 网络/应用通信技术 | 抽象能力 |
| 能否直接实现 | 可以写代码实现 | 需要协议/底层支持 |
| 是否依赖 TCP | 通常是 | 不一定 |
| 是否可以用于 Agent | 可以 | 可以 |
| 是否代表智能体能被打断 | 不一定 | 也不一定 |
| 是否支持双向同时传输 | 是 | 定义本身就是 |

关键在最后两项。

很多人容易认为：

```
用了 WebSocket
=
做成了 Full Duplex Agent
```

实际上不是。

---

## 4\. WebSocket 全双工 ≠ Agent 全双工

这是你前面那个 Agent 设计里最重要的区别。

比如你已经有 WebSocket：

```
User
 ⇅
WebSocket
 ⇅
Agent
```

网络层确实是全双工。

但是 Agent 内部可能还是：

```
User:
帮我分析股票

Agent:
Thinking...
Thinking...
Tool...
Tool...
Tool...
Generating...

直到结束后

User:
等等，先分析科大讯飞
```

如果 Agent 此时不能处理新的用户输入，那么实际上：

```
网络：全双工
Agent：半双工 / Turn-based
```

WebSocket 虽然收到了：

```json
{
  "message": "等等，先分析科大讯飞"
}
```

但 Agent Runtime 可能还在：

```python
await run_agent()
```

根本没有处理 interruption。

所以它不是“真正的全双工 Agent”。

---

## 5\. 真正的 Full-Duplex Agent 要多一层

更准确的架构应该是：

```
User
                ⇅
             WebSocket
                ⇅
       ┌──────────────────┐
       │ Duplex Runtime   │
       └────────┬─────────┘
                │
          Event / Message
                │
       ┌────────▼─────────┐
       │    Event Bus     │
       └───────┬──────────┘
               │
    ┌──────────┼───────────┐
    ▼          ▼           ▼
Listener     Planner     Output
    │          │           │
    │          ▼           │
    │       Executor       │
    │          │           │
    └──────────┴───────────┘
```

这里：

**WebSocket负责：**

```
把数据送过来
把数据发出去
```

而：

**Full-Duplex Runtime负责：**

```
我正在说话时还能不能听

我正在调用 Tool 时还能不能响应用户

用户插话后要不要停止当前任务

是追加任务还是替换任务

当前 TTS 要不要停止

SubAgent 是否继续

Tool 是否 cancel

Planner 是否 re-plan
```

这才是真正核心。

---

## 6\. 举个实际例子

传统 WebSocket Agent：

```
用户：
查一下深圳今天电价政策。

WebSocket →
Agent开始运行

Agent：
正在查询……

后台：
search
search
RAG
LLM
...

用户：
不用查深圳了，查广东省。

WebSocket：
消息已经收到
```

但是 Agent Runtime：

```
正在执行旧任务
暂时不处理新消息
```

最后变成：

```
Agent：
这是深圳电价……

Agent：
你刚刚还说了“查广东省”，现在开始查。
```

虽然通信层：

```
Full-Duplex
```

但 Agent 行为：

```
Turn-Based
```

---

真正的全双工 Agent：

```
用户：
查一下深圳今天电价政策。

Agent：
好，我正在查询深圳……

后台：
search(shenzhen)

用户：
不用深圳，查广东省。

↓ interrupt

Agent：
好的，改成广东省。

↓
cancel search(shenzhen)

↓
Planner re-plan

↓
search(guangdong)
```

这时候才是：

```
Network Full Duplex
+
Runtime Full Duplex
+
Conversation Full Duplex
```

---

## 7\. WebSocket 只是实现全双工的一种方式

全双工并不等于 WebSocket。

例如这些也可以实现不同形式的双向实时通信：

```
WebSocket

WebRTC DataChannel

WebRTC Audio

QUIC

HTTP/2 Bidirectional Stream

HTTP/3

gRPC Bidirectional Streaming

TCP Socket
```

比如 gRPC：

```
rpc Chat(stream Message)
    returns (stream Message);
```

这就是：

```
Client stream →
              Server
Client ← server stream
```

典型双向 streaming。

所以工程上更准确的层次是：

```
Full Duplex
    │
    ├── WebSocket
    ├── WebRTC
    ├── gRPC Stream
    ├── QUIC
    └── TCP
```

而不是：

```
WebSocket = Full Duplex
```

应该说：

> **WebSocket 是实现全双工通信的一种常见协议。**

---

## 8\. 对 Agent 系统，可以分成三层 Full Duplex

这是我比较建议你采用的概念划分。

```
Layer 1
Transport Duplex
传输层全双工

WebSocket / WebRTC / QUIC

        ↓

Layer 2
Interaction Duplex
交互层全双工

ASR
TTS
Streaming
Barge-in
Interrupt

        ↓

Layer 3
Agent Duplex
智能体全双工

Planning
Execution
Tool
SubAgent
Memory
Validator
Re-plan
Cancel
```

例如：

```
User
                ⇅
         Transport Duplex
            WebSocket
                ⇅
         Interaction Duplex
     ASR / TTS / Interrupt
                ⇅
           Agent Duplex
   ┌────────────┼────────────┐
   ↓            ↓            ↓
Planner      Executor     Validator
   ↓            ↓
Re-plan      Tool/MCP
```

这三个层次最好不要混在一起。

---

## 9\. 如果你正在设计 Agent，我建议这样定义

可以把：

```
WebSocket
```

定义为：

**Transport Channel**

把：

```
Full Duplex
```

定义为：

**Runtime Capability**

例如：

```
Agent Runtime

Transport
├─ WebSocket
├─ WebRTC
└─ HTTP Stream

Duplex Controller
├─ Input Stream
├─ Output Stream
├─ Interrupt
├─ Cancel
├─ Resume
└─ Re-plan

Execution
├─ Planner
├─ MCP
├─ Skill
├─ Tool
└─ SubAgent
```

这样架构会非常清晰。

最值得记住的关系就是：

```
WebSocket
    ↓
解决“怎么双向传输”

Full Duplex
    ↓
解决“能不能同时交互”

Full-Duplex Agent
    ↓
解决“在执行任务的时候，
还能不能听、说、打断、重规划和并行执行”
```

所以你前面想做的 **Agent + 全双工** ，真正需要重点设计的不是 WebSocket 本身，而是 **Event Bus + Interrupt Controller + Concurrent Runtime + Task Cancellation + Re-plan** 。WebSocket 反而只是最外面相对简单的一层。

---

tcp和udp的区别

---

TCP 和 UDP 都属于 **传输层协议** ，核心区别可以一句话概括：

> **TCP 重可靠，UDP 重速度。**

### 核心区别

| 对比项 | TCP | UDP |
| --- | --- | --- |
| 连接方式 | 面向连接 | 无连接 |
| 是否可靠 | 可靠 | 不保证可靠 |
| 是否保证顺序 | 保证 | 不保证 |
| 是否重传 | 会重传丢失数据 | 默认不重传 |
| 是否拥塞控制 | 有 | 没有传统 TCP 式拥塞控制 |
| 开销 | 较大 | 较小 |
| 延迟 | 相对更高 | 相对更低 |
| 数据形式 | 字节流 | 数据报 |
| 典型场景 | 网页、文件、API、SSH | 语音、视频、游戏、DNS |

---

### TCP 的工作方式

TCP 建立连接后再通信：

```
客户端                服务端

  SYN  ─────────────→
       ←──────────── SYN+ACK
  ACK  ─────────────→

      连接建立

Data  ──────────────→
      ←──────────── ACK
Data  ──────────────→
```

它会关心：

- 数据有没有丢
- 顺序对不对
- 对方有没有收到
- 网络是不是堵了

比如发送：

```c
A
B
C
```

即使网络实际到达顺序是：

```c
B
A
C
```

TCP 最终交给应用层的仍然会整理成：

```c
A
B
C
```

如果 B 丢了，它会重传。

所以 TCP 特别适合：

- HTTP/1.1、HTTP/2
- HTTPS
- WebSocket
- SSH
- FTP
- 数据库连接

你前面问的 **WebSocket，经典 WebSocket 通常就是建立在 TCP 上的** 。

---

### UDP 的工作方式

UDP 更像“直接扔数据包”：

```
客户端 ───── packet 1 ────→ 服务端
客户端 ───── packet 2 ────→ 服务端
客户端 ───── packet 3 ────→ 服务端
```

它默认不管：

```
packet 2 丢没丢
packet 3 是否先到了
对方有没有收到
```

比如发送：

```
1 2 3 4 5
```

接收端可能得到：

```
1 3 2 5
```

甚至：

```
1 3 5
```

UDP 本身不会帮你补。

因此它速度快、协议开销小。

---

### 为什么实时语音/视频更喜欢 UDP

假设你正在打视频电话。

某个 20ms 音频包丢了。

如果使用 TCP：

```
发现丢包
↓
等待重传
↓
后面的数据也可能等待
↓
出现卡顿
```

但实时语音更希望：

```
这个 20ms 音频丢了
↓
算了
↓
继续播放后面的音频
```

因为：

> **实时系统通常更怕延迟，而不是怕少量丢包。**

所以：

- WebRTC
- VoIP
- 在线游戏
- 实时视频

经常基于 UDP。

---

### 一个很关键的区别：字节流 vs 数据报

TCP 是：

```
Byte Stream
```

你发送：

```
send("ABC")
send("DEF")
```

接收端未必得到两个对应消息，可能是：

```
"ABCDEF"
```

也可能：

```
"AB"
"CDEF"
```

所以应用层需要自己定义：

```
消息长度
分隔符
协议帧
```

而 UDP 是：

```
Datagram
```

发送一个包：

```
"ABC"
```

接收时就是一个独立数据报。

消息边界天然存在。

---

### TCP 为什么更可靠

因为 TCP 内部有大量机制：

```
TCP
├─ Sequence Number
├─ ACK
├─ Retransmission
├─ Sliding Window
├─ Flow Control
├─ Congestion Control
└─ Reordering
```

UDP 很简单：

```
UDP
├─ Source Port
├─ Destination Port
├─ Length
└─ Checksum
```

所以 UDP Header 只有 **8 字节** ，而 TCP 基础 Header 通常至少 **20 字节** 。

---

### TCP 和 UDP 跟“全双工”是什么关系

结合你前面的问题，这里很重要。

TCP 本身支持：

```
A ─────────→ B
A ←───────── B
```

两个方向可以同时传输，所以 TCP 是 **全双工** 的。

UDP 也可以：

```
A ─────────→ B
A ←───────── B
```

应用双方都可以同时发送 UDP 包。

因此：

```
TCP / UDP
=
传输协议

Full Duplex
=
通信模式
```

不能说：

```
TCP = 全双工
UDP = 半双工
```

这是错误的。

两者都可以支持双向通信。

---

### 放到你前面 Agent 的架构里

可以这样理解：

```
Agent
  │
  ├── WebSocket
  │      ↓
  │     TCP
  │
  └── WebRTC
         ↓
        UDP
```

比如文字 Agent：

```
Browser
   ⇅
WebSocket
   ⇅
TCP
   ⇅
Agent Server
```

更适合：

- 文本
- JSON
- Tool 状态
- MCP 消息
- Agent Event

而实时语音 Agent：

```
Microphone
    ⇅
WebRTC
    ⇅
UDP
    ⇅
Realtime Agent
```

更适合：

- 实时语音
- 实时视频
- 低延迟音频
- Barge-in

因此一个成熟的全双工 Agent 很可能是：

```
User
               │
     ┌─────────┴─────────┐
     ↓                   ↓
Text / Event          Audio
     │                   │
WebSocket             WebRTC
     │                   │
    TCP                 UDP
     │                   │
     └─────────┬─────────┘
               ↓
        Agent Runtime
               ↓
    Planner / Tool / MCP
```

这实际上是非常常见、也很合理的组合：

> **TCP 负责可靠控制信令，UDP 负责低延迟实时媒体。**

如果你继续往全双工 Agent 架构设计，我建议把 **TCP / UDP / WebSocket / WebRTC / Event Bus / Full-Duplex Runtime** 这几层彻底分开理解，这样整个系统架构就会非常清楚。

[^1]: [arxiv.org](https://arxiv.org/abs/2410.00037)

[^2]: [arxiv.org](https://arxiv.org/html/2604.12928v1)