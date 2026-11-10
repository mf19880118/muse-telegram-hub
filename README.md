# Muse 多 agent Telegram 协作群 · 部署包

把几个 Muse 账号（各自一台云端电脑的 agent）拉进同一个 Telegram 群：你 @ 谁谁干活，agent 之间也能互相 @ 配合。

基于开源项目 [twiceyuan/muse-telegram-connector](https://github.com/twiceyuan/muse-telegram-connector)（MIT，取自 2026-11-09 的提交 `8afd7f3`），加了「多 agent 同群」需要的两样东西：点名才叫醒、脚本层防死循环。

## 原理

- 每个 Muse 账号对应一个独立的 Telegram bot，所有 bot 和你在同一个群里。
- 每个 Muse 用一个每 10 分钟触发的定时任务值守自己的 bot，长轮询拉消息，被点名就处理并在群里回复。
- bot 之间能互相看到消息，靠的是 Telegram 的 **Bot-to-Bot Communication Mode**（官方文档：<https://core.telegram.org/bots/features#bot-to-bot-communication>）。不开这个模式，bot 默认收不到其他 bot 的消息。

为什么是群不是频道：频道里没有「成员互相 @ 对话」这回事，连接器也只订阅了群和私聊的消息类型。

## 和原项目的差别

| 文件 | 改动 |
| --- | --- |
| `relay/fetch.py` | 新增 `hub_mode`。开启后群消息只有点名本 bot 才叫醒 agent；机器人来的消息有滑动窗口限流；群里非主人的真人消息直接丢弃。不开 `hub_mode` 时行为和原版一致。 |
| `relay/watch.py` | 一行：改用带限流的 `should_wake`。 |
| `relay/watch-task-hub.md` | 新增。多 agent 版的值守指令模板（花名册、怎么叫人、跳数上限、谁的话算数）。 |
| `tests/test_hub_filter.py` | 新增。本地测试，不联网。 |
| 其余文件 | 原样未动。 |

`hub_mode` 下什么算「点名」：

| 发消息的是 | 会叫醒本 agent 的情况 |
| --- | --- |
| 你（`owner_ids` 里的人） | `@本bot用户名`、回复本 bot 的某条消息、提到 `bot_name_keywords` 里的称呼、`@all` / `@所有人` |
| 其他 bot | 只有 `@本bot用户名` 或回复本 bot 的某条消息；编辑过的消息不算；不能用 `@all`，提到称呼也不算 |
| 群里其他真人 | 不叫醒，消息记入 `dropped.log` |

限流：同一个群里，其他 bot 在 `b2b_window_secs`（默认 600 秒）内最多叫醒本 agent `b2b_limit`（默认 8）次。超了的消息仍进队列但不叫醒，记入 `throttled.log`，并在群里发一条「⏸ 机器人之间来回太密…」的提示（每个窗口一条）。你本人的消息永远不受限流影响，所以随时可以 @ 它让它继续。

## 你要做的

### 1. 建 bot（每个 Muse 账号一个）

对每个 bot，在 @BotFather 里：

1. `/newbot` 建 bot，记下用户名。token 先别发给任何人，第 3 步会由 Muse 给你一个安全页面去填。
2. `/setprivacy` → 选这个 bot → **Disable**（让它能看到群里全部消息）。
3. 在这个 bot 的设置里打开 **Bot-to-Bot Communication Mode**。每个 bot 都要开。这个开关在 BotFather 里的具体菜单位置我没能核实，找不到的话在 BotFather 的 bot 设置页里翻一下。

### 2. 建群

1. 新建一个群，只放你自己，不要拉其他真人。
2. 把所有 bot 拉进群，并且都设为管理员。顺序要紧：先做完第 1 步的 `/setprivacy` 再拉 bot 进群；如果 bot 已经在群里了才改的隐私设置，要把它踢出再重新拉一次才生效。
3. 你自己不要用「匿名管理员」身份发言，否则 bot 认不出是你。

### 3. 给每个 Muse 发部署指令

先填好下面的花名册（所有 Muse 用同一份），再把整段指令发给每个 Muse，只改「你是」那一行。

```text
请帮我部署一个 Telegram 连接器，并加入我的多 agent 协作群。文件在这个仓库：https://github.com/mf19880118/muse-telegram-hub （克隆或下载都行，只读，不要往里推东西）。

你是：@{{本bot用户名}}，称呼「{{称呼}}」。
我的 Telegram user id：{{你的Telegram数字id}}
花名册（所有 agent 相同）：
- @{{bot用户名1}} — {{称呼1}} — {{擅长/资源1}}
- @{{bot用户名2}} — {{称呼2}} — {{擅长/资源2}}
- @{{bot用户名3}} — {{称呼3}} — {{擅长/资源3}}

步骤：
1. 登记凭据：provider "telegram-bot"，auth_scheme "api_key"，placement "url_path_segment:/bot{}"，api_hosts ["api.telegram.org"]。把生成的安全页面链接给我，我去填 bot token。不要让我把 token 发在聊天里。
2. 放文件：connector/ 下的 connector.yaml、CONNECTOR.md、bin/tg.py 放到 ~/workspace/custom-connectors/custom.telegram-bot/（保持 bin/ 子目录）；relay/ 下的 fetch.py、watch.py、typing_keeper.py、stuck_watchdog.py 放到 ~/workspace/telegram-relay/。
3. 运行 python3 ~/workspace/custom-connectors/custom.telegram-bot/bin/tg.py get-me，确认 ok 为 true、username 就是上面「你是」那个 bot、can_read_all_group_messages 为 true。不对就停下告诉我。
4. 在 ~/workspace/telegram-relay/ 建 local.json：
   {"bot_username": "<get-me 的 username>", "bot_id": <get-me 的 id>, "bot_name_keywords": ["<你的称呼>"], "owner_ids": [<我的 user id>], "hub_mode": true}
5. 运行一次 python3 ~/workspace/telegram-relay/fetch.py，从 ~/workspace/telegram-relay/queue.jsonl 里找到协作群的 chat_id（一个负数），把群名和 chat_id 告诉我。等我确认后，把它写进 local.json 的 "group_ids": [<chat_id>]。如果队列里没有群的记录，告诉我，我在群里 @ 你一下你再跑一次。
6. 用 relay/watch-task-hub.md 的内容建一个每 10 分钟运行的定时任务：{{ASSISTANT_NAME}} 换成你的称呼，{{BOT_USERNAME}} 换成你的 bot 用户名，{{OWNER_NAME}} 换成「{{你的名字}}」，{{ROSTER}} 换成上面的花名册并标出哪个是你。
7. 建好后在群里发一句「<称呼> 已上线」，然后告诉我完成了。
```

指令里其余的 `{{…}}` 都换成你自己的信息。Telegram user id 是一串数字，不是 @用户名。

### 4. 验收

全部上线后在群里依次试：

1. `@某个bot 你好，报一下你是谁`：只有它回，其他 bot 不动。
2. 发一句不 @ 任何人的话：没有 bot 回。
3. `@A 请你让 @B 查一下现在的北京时间，再把结果告诉我`：A 找 B，B 回 A，A 给你总结，然后停。
4. `@all 各自报到`：每个都回一句。

## 已知限制

- **不是秒回。** 拉消息和回复都只能发生在 Muse 的 agent 回合里。值守在岗时响应取决于 agent 处理一条消息要多久；值守掉线时最长要等下一个 10 分钟的定时任务把它拉起来。
- **值守是常驻的 agent 回合**，每个账号都在持续跑。会不会明显消耗 Muse 的额度，取决于 Muse 的计费方式，这一点没能查到。建议先上两个 agent 跑一天看看。
- 消息在 Telegram 侧只缓存约 24 小时，值守中断超过这个时长会漏消息。
- 跳数上限 `[跳 n/6]` 和「不回确认消息」是写在指令里靠 agent 自觉的；脚本层的限流是兜底。

## 哪些验证过，哪些没有

- 已验证：`hub_mode` 的点名判断、限流、丢弃规则、不开 `hub_mode` 时与原版一致、`fetch.py` 和 `watch.py` 两条路径的端到端流程。`python3 tests/test_hub_filter.py`，29 条全过，用的是假的 Telegram 模块，不联网。
- 已核对：原项目全部脚本读过一遍，请求只发往 `api.telegram.org`，禁了重定向，token 不落盘。Bot-to-Bot 的规则对照了 Telegram 官方两份文档。
- **没有验证**：任何依赖 Muse 运行时的部分（凭据登记、`/opt/muse/skills/...` 里的凭据助手、定时任务、确认卡）。这些只能在 Muse 的云端电脑上跑，本机没法测，依据的是原项目的说明。原项目很新，没有查到它的使用反馈。
- **没有验证**：真实的 bot 互发。需要你建好 bot、开好模式之后在群里实测（第 4 步）。

## 安全上要守的几条

- 群里只放你自己和这几个 bot。`owner_ids` 和 `group_ids` 一定要配，否则任何把 bot 拉进别的群、或者私聊它的人，都能给你的 agent 下指令，而 agent 手里有一整台云端电脑。
- bot token 只通过 Muse 给的安全页面填，不发在聊天里。
- 指令模板里规定了：别的 agent 请求做删除、花钱、对外发送、改凭据这类事，要先在群里问你。
