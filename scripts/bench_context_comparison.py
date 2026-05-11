#!/usr/bin/env python3
"""
Benchmark: OLD (full transcript) vs NEW (summary + recent window) context building.

Measures TTFT and TPS for the same questions under both modes,
demonstrating the impact of progressive context compression.

Usage:
    python scripts/bench_context_comparison.py [--base-url http://localhost:8000] [--model qwen3.5:9b]
"""

import argparse
import json
import time
import sys

import httpx


# ── Simulated 30-minute meeting transcript (~10k tokens) ──────────────────

TRANSCRIPT_SEGMENTS = [
    # Product planning meeting (t=0s ~ t=1800s, 30 minutes)
    ("主持人A", 0, 8, "各位早上好，今天我们讨论Q3的产品路线图。大家先汇报各自领域的进展。"),
    ("产品经理B", 8, 18, "好的。我们重点关注三件事：用户增长、付费转化和留存率。上季度用户增长只完成了目标的78%，主要原因是我们拉新渠道太单一。"),
    ("技术负责人C", 18, 28, "后端架构需要重构，目前的单体应用扩展性不够。上周数据库连接池已经到了上限，API响应时间P99到了800ms。"),
    ("设计师D", 28, 38, "我建议把首页UI完全重新设计，增加个性化推荐区。用户调研显示，60%的用户认为当前首页内容与他们的兴趣不相关。"),
    ("数据分析E", 38, 48, "上季度数据显示移动端转化比PC低40%，需要重点优化。而且移动端的跳出率高达65%，远高于PC端的35%。"),
    ("主持人A", 48, 55, "大家的意见都很好。我们逐一讨论优先级。"),
    ("主持人A", 55, 65, "第一个议题：用户增长。B你觉得拉新渠道应该怎么拓展？"),
    ("产品经理B", 65, 80, "我建议同时开拓三个渠道：短视频平台投放、KOL合作、以及企业微信私域运营。短视频CPM比SEM低60%，ROI更好。"),
    ("技术负责人C", 80, 90, "短视频投放需要配套的数据追踪SDK，这块需要两周开发时间。另外需要埋点体系升级。"),
    ("数据分析E", 90, 102, "我补充一下，根据竞品分析，竞品X上季度短视频渠道带来了30%的新用户，而我们只有5%。差距很大。"),
    ("设计师D", 102, 112, "短视频素材我可以负责，但是需要两周时间准备。建议先做三套方案：产品介绍、用户案例、功能演示。"),
    ("主持人A", 112, 120, "好的，增长方案基本明确了。下一个议题：付费转化。"),
    ("产品经理B", 120, 138, "付费转化的核心问题是定价策略。当前我们只有一个定价方案，但用户画像显示有三类群体：个人用户、小团队、企业客户。他们的支付意愿差异很大。我建议做分层定价。"),
    ("技术负责人C", 138, 148, "分层定价需要改造计费系统，工作量大概三周。还要考虑老用户的迁移方案，不能影响现有付费用户。"),
    ("数据分析E", 148, 160, "AB测试数据显示，如果把小团队套餐价格从99降到79，转化率能提升25%，总收入反而增长8%。建议先从这个点切入。"),
    ("设计师D", 160, 170, "定价页面的UI也需要重新设计，要突出分层对比。我可以出一版参考Notion的定价页面。"),
    ("主持人A", 170, 180, "定价策略很重要，B你牵头出个详细方案。下一个：留存率。"),
    ("产品经理B", 180, 200, "留存率是最大的挑战。当前7日留存只有18%，行业平均是30%。我发现一个关键问题：新用户首次使用产品的引导流程太复杂，40%的用户在注册后没有完成第一次核心操作就流失了。"),
    ("技术负责人C", 200, 210, "新手引导需要重构，目前的引导是硬编码的，没法AB测试。我建议用特性开关系统来做，这样可以灵活调整。"),
    ("数据分析E", 210, 225, "我做过漏斗分析，发现最严重的流失节点是第三步：配置工作区。70%的用户在这一步放弃。建议简化到只需要两步就能开始使用。"),
    ("设计师D", 225, 240, "同意。我设计一版极简的onboarding流程：第一步选用途，第二步直接进入工作台。所有高级配置放到设置里。"),
    ("主持人A", 240, 255, "好，现在来定优先级。大家投票：增长、转化、留存，哪个最优先？"),
    ("产品经理B", 255, 270, "我提议先做留存率，因为获客成本最近翻倍，如果留存不改善，花钱拉新等于往漏斗里倒水。"),
    ("技术负责人C", 270, 285, "同意留存优先，但是架构重构不能拖。我建议并行：主力做留存优化，同时抽出20%精力做架构准备。"),
    ("设计师D", 285, 295, "留存相关的UI改动我可以优先支持。onboarding重新设计大概需要一周。"),
    ("数据分析E", 295, 305, "我会建立留存率的实时监控大盘，每天跟踪关键指标。另外建议每周出一份留存分析报告。"),
    ("主持人A", 305, 320, "好，我们分两组并行推进。架构组和留存组。架构组由C负责，留存组由B负责，D和E两边都支持。"),
    ("主持人A", 320, 335, "总结一下今天的决定：第一，Q3核心目标是提升留存率到25%；第二，分层定价方案两周内出初稿；第三，短视频增长渠道本月启动测试。"),
    ("主持人A", 335, 345, "下次会议时间定在下周三，届时review各组进展。散会，谢谢大家。"),
    # ── Extended discussion: deep-dive on retention (t=345s ~ t=600s) ──
    ("产品经理B", 345, 365, "补充一下留存的具体方案。第一步是优化新用户onboarding流程，把三步合并为两步：选择用途模板，然后直接进入工作台。第二步是增加每日任务提醒，用推送通知拉回沉默用户。"),
    ("技术负责人C", 365, 380, "推送通知需要接入APNs和FCM，后端大概一周。另外onboarding流程改造需要前端配合，当前的引导组件是硬编码的，需要做成可配置的。"),
    ("设计师D", 380, 400, "我已经出了新版onboarding的初稿。主色调改为蓝白，更清爽。第一步是一个大卡片，三个选项：个人学习、团队协作、企业办公。选完后直接加载对应的模板工作区，不需要任何额外配置。"),
    ("数据分析E", 400, 415, "我建议在onboarding里埋一个关键事件：用户创建第一个内容。数据显示，完成这个动作的用户7日留存达到45%，没完成的只有8%。"),
    ("产品经理B", 415, 435, "很好的洞察。所以onboarding的终极目标就是把用户带到创建第一个内容这一步。我们可以在引导流程中加一个奖励机制：完成首次创建送7天VIP体验。"),
    ("技术负责人C", 435, 450, "VIP体验需要计费系统支持，但现在分层定价还没上线，可以先用一个临时的feature flag来控制VIP权限，等定价系统改造完再迁移。"),
    ("设计师D", 450, 465, "奖励弹窗的设计我会出一版，风格参考Duolingo的成就徽章。要让用户感觉有成就感，而不是被推销。"),
    ("数据分析E", 465, 480, "另外，关于留存还有一个数据点：每周打开3次以上的用户，30日留存高达70%。所以除了onboarding，还需要设计一个每日打开的习惯养成机制。"),
    ("主持人A", 480, 495, "同意，习惯养成是关键。B你来设计这个每日机制的具体形式。"),
    ("产品经理B", 495, 520, "我的想法是做一个每日会议摘要卡片。每天上午10点推送前一日的会议要点和待办事项，让用户养成每天打开看一眼的习惯。同时可以结合团队协作功能，@提醒未读的同事。"),
    ("技术负责人C", 520, 535, "每日摘要推送需要一个定时任务服务。可以用Redis的延迟队列来实现。另外需要一个模板引擎来生成个性化的摘要卡片。"),
    ("设计师D", 535, 555, "摘要卡片我设计了两版：一版是纯文本列表，简洁直接；另一版是卡片式，带头像和颜色标签。我倾向卡片式，视觉上更有吸引力。"),
    ("数据分析E", 555, 570, "建议两版都做AB测试。根据过往数据，卡片式CTR比纯文本高20%，但打开后的完成率纯文本更高。需要看具体数据再决定。"),
    ("主持人A", 570, 585, "好，onboarding优化和每日摘要这两个机制都安排上。接下来讨论增长渠道的执行细节。"),
    # ── Growth channel details (t=585s ~ t=900s) ──
    ("产品经理B", 585, 610, "短视频投放的具体方案：第一批投放抖音和B站，目标人群25-35岁职场人士。素材方向三个：痛点场景剧（展示会议记录的混乱），产品演示（30秒快速功能展示），用户证言（真实用户的使用故事）。"),
    ("设计师D", 610, 630, "素材制作我来协调。痛点场景剧可以外包给短视频制作公司，费用大约每条5000元。产品演示和用户证言可以自己拍摄，我来负责脚本和分镜。"),
    ("技术负责人C", 630, 650, "投放追踪SDK我推荐用AppsFlyer或者Adjust，接入大概一周。自建的话需要两周，而且维护成本高。建议先用第三方，等量起来再考虑自建。"),
    ("数据分析E", 650, 670, "关于投放预算，我建议先小规模测试：抖音每天2000元，B站每天1000元，跑两周看数据。重点关注CPI和7日留存，如果CPI超过15元或者7日留存低于15%，就暂停优化。"),
    ("主持人A", 670, 685, "预算审批我去跟VP沟通。KOL合作这块谁来负责？"),
    ("产品经理B", 685, 710, "KOL合作我来牵头。我已经整理了一个目标KOL清单，分三层：头部KOL（粉丝100万+，预算3万/条），腰部KOL（10-100万粉丝，预算5000-1万/条），素人（1万粉丝以下，产品置换）。"),
    ("设计师D", 710, 725, "KOL合作需要准备一个媒体包：产品截图、核心卖点文案、品牌指南。我下周可以出一版。"),
    ("技术负责人C", 725, 745, "私域运营需要一个企业微信的自动化工具。我调研了两个方案：微伴助手和句子互动。微伴的功能更全但价格高（年费3万），句子互动便宜（年费8000）但功能少。建议先用句子互动，够用就好。"),
    ("数据分析E", 745, 760, "三个渠道我建议分开追踪效果。短视频用UTM参数，KOL用专属邀请码，私域用渠道码。这样可以准确计算每个渠道的ROI。"),
    ("主持人A", 760, 775, "好，增长方案就按这个执行。下周三前各组出详细计划。接下来说说Q4的长远规划。"),
    # ── Q4 planning (t=775s ~ t=1000s) ──
    ("产品经理B", 775, 800, "Q4有两个大方向可以考虑：国际化和AI功能。国际化需要本地化团队，投入大但市场广阔。AI功能可以用现有的LLM API快速落地，比如智能会议摘要、自动任务提取。"),
    ("技术负责人C", 800, 825, "AI功能落地更快。我们可以用LangChain + GPT-4实现三个功能：实时会议摘要、智能问答、自动任务分配。技术原型大概一个月能做出来。"),
    ("设计师D", 825, 845, "AI功能的UI设计需要仔细考虑交互方式。我觉得应该做成无感融入的：在会议进行中实时显示摘要侧边栏，结束后自动生成任务清单，不需要用户手动触发。"),
    ("数据分析E", 845, 865, "竞品分析显示，已经有3家竞品在做AI会议助手了。其中竞品Y的AI功能上线后，付费转化率提升了15%。这是一个很值得投入的方向。"),
    ("产品经理B", 865, 890, "我同意AI方向。但国际化也不能完全搁置。建议Q4先做AI功能MVP，同时做国际化的技术调研，Q1再正式启动国际化。"),
    ("技术负责人C", 890, 910, "AI功能如果要快速上线，需要解决三个技术问题：LLM的成本控制、响应延迟、以及数据隐私。成本方面，每次会议摘要大约消耗5000 tokens，按GPT-4的价格大约0.15元。"),
    ("主持人A", 910, 930, "AI功能的成本可以转嫁给用户，作为一个高级功能。但数据隐私必须处理好，企业客户对这个非常敏感。"),
    ("数据分析E", 930, 950, "我建议做两版方案：一版用云端LLM（面向个人和小团队），一版支持私有化部署（面向企业客户）。这样可以覆盖不同用户群的需求。"),
    ("设计师D", 950, 970, "AI功能的界面我会做两种风格：一种是嵌入式（融入现有UI），一种是独立面板（类似ChatGPT的对话框）。我先出mockup给大家看看。"),
    ("主持人A", 970, 990, "好，AI功能作为Q4重点项目立项。B你来写PRD，C出技术方案，D做UI设计，E做竞品分析报告。"),
    ("主持人A", 990, 1010, "最后补充一点：Q3的周报机制从本周开始执行。每周五下午4点前各组提交进展，我来汇总。"),
    ("产品经理B", 1010, 1025, "收到。我建议周报用一个统一的模板：本周完成、下周计划、风险和阻塞项。这样汇总效率高。"),
    ("技术负责人C", 1025, 1040, "同意。技术组的周报我会加上代码review情况和线上bug统计。另外建议每周二加一个15分钟的技术同步会。"),
    ("主持人A", 1040, 1055, "好，周报模板和周二技术同步会都安排上。今天的会议内容非常充实，感谢大家的投入。"),
    ("主持人A", 1055, 1065, "散会。下周三同一时间见。"),
    # ── Second half: technical deep-dive (t=1065s ~ t=2400s, another ~20min) ──
    ("技术负责人C", 1065, 1090, "我来汇报一下技术架构的详细方案。单体应用拆分分为三个阶段：第一阶段，把用户服务和支付服务独立出来；第二阶段，消息推送和通知服务独立；第三阶段，核心业务逻辑拆分为微服务。"),
    ("产品经理B", 1090, 1110, "第一阶段的用户服务独立需要多久？我需要评估对Q3留存优化的时间影响。另外支付服务独立会影响分层定价的上线时间吗？"),
    ("技术负责人C", 1110, 1135, "用户服务独立大概两周，主要是数据库拆分和接口改造。支付服务更复杂，需要三周，因为涉及第三方支付渠道的对接变更。分层定价上线前必须先完成支付服务独立。"),
    ("设计师D", 1135, 1155, "关于onboarding新设计的细节补充：第一步的选择卡片做了三种视觉方案。方案A用图标+文字，方案B用截图预览，方案C用动画演示。我建议用方案B，信息量最大且加载最快。"),
    ("数据分析E", 1155, 1175, "AB测试计划已经准备好了。测试组和对照组各5000用户，主要指标是第一步完成率和7日留存。预计两周可以收集到有统计显著性的数据。"),
    ("主持人A", 1175, 1200, "好的。关于数据隐私的合规问题，我们需要请法务团队审查AI功能的数据处理流程。特别是GDPR和中国个人信息保护法的要求。"),
    ("产品经理B", 1200, 1225, "我已经准备了一份数据流图，标注了数据从采集到存储到LLM处理的全流程。关键问题是：用户会议内容是否需要加密存储？是否需要用户明确同意才能用于AI处理？"),
    ("技术负责人C", 1225, 1250, "技术上，我建议所有用户数据默认加密存储，LLM调用时临时解密。另外要实现数据保留策略：个人数据30天自动删除，企业客户可以自定义保留期。"),
    ("设计师D", 1250, 1270, "隐私设置页面我设计了一个简洁的控制面板：三个开关分别是数据收集、AI功能、匿名化统计。默认只有数据收集开启，其他需要用户主动同意。"),
    ("数据分析E", 1270, 1290, "关于匿名化统计，我建议用差分隐私技术。这样我们可以在不暴露个人数据的情况下分析用户行为趋势。实现成本大约一周。"),
    ("主持人A", 1290, 1310, "隐私合规这块很重要，B和C你们两个牵头，下周出一份详细的数据处理合规方案，给法务审查。"),
    ("产品经理B", 1310, 1340, "好的。另外关于国际化，我做了一个初步调研。东南亚市场对会议工具的需求增长最快，但竞争也激烈。日本市场客单价高但进入门槛高。建议第一站选英语市场，技术改造最少。"),
    ("技术负责人C", 1340, 1365, "国际化技术改造主要是三块：UI多语言（i18n框架改造，约两周），时区处理（影响所有时间相关的功能，约一周），支付渠道（需要对接Stripe国际支付，约两周）。"),
    ("设计师D", 1365, 1385, "多语言UI需要考虑文字长度差异。德语比中文长50%，日语需要更大的字体。我建议用弹性布局，不要固定宽高。另外RTL语言（阿拉伯语、希伯来语）需要镜像布局。"),
    ("数据分析E", 1385, 1410, "竞品在东南亚的定价策略值得关注。竞品Z在印尼的价格是中国的1/3，但在日本是中国的1.5倍。定价本地化是国际化成功的关键因素之一。"),
    ("主持人A", 1410, 1430, "国际化的讨论先到这里，Q4再深入。现在回到Q3的执行计划，B你来过一下甘特图。"),
    ("产品经理B", 1430, 1470, "Q3执行计划的时间线如下：第1-2周完成onboarding优化设计和技术方案，第3-4周开发和AB测试，第5周上线。同步进行的有：第1-3周分层定价方案设计，第4-6周计费系统改造，第7周灰度发布，第8周全量。短视频投放从第2周开始测试。"),
    ("技术负责人C", 1470, 1500, "技术资源分配：2个后端负责架构拆分和支付改造，2个前端负责onboarding和定价页面，1个移动端负责推送通知。还需要1个数据工程师支持AB测试和监控大盘。总人力8人，基本满负荷。"),
    ("设计师D", 1500, 1520, "设计资源紧张。onboarding设计、定价页面、隐私设置、短视频素材、KOL媒体包，这些加起来至少需要3周。建议外包短视频素材制作，自己专注产品UI。"),
    ("数据分析E", 1520, 1545, "监控体系我已经搭好了框架。关键指标看板包括：DAU、留存率（1日/7日/30日）、转化率（注册→激活→付费）、流失预警（3天未登录触发提醒）。数据延迟控制在5分钟内。"),
    ("主持人A", 1545, 1570, "很好。风险点识别：1）架构重构可能引入新bug，需要完善的回归测试；2）AB测试样本量可能不够，需要延长测试时间；3）短视频投放ROI不确定，需要设止损线。"),
    ("产品经理B", 1570, 1600, "关于止损线：如果两周内短视频CPI超过15元，或者7日留存低于15%，就暂停投放优化素材。KOL合作如果30天内ROI低于1:2，就调整合作策略从头部转向腰部。"),
    ("技术负责人C", 1600, 1625, "回归测试方面，我建议投资自动化测试。目前测试覆盖率只有40%，需要提升到80%以上。关键路径的E2E测试优先级最高，包括注册流程、支付流程、核心功能操作。"),
    ("设计师D", 1625, 1645, "关于品牌一致性，我做了一份设计规范文档，包括颜色系统、字体规范、间距规则、组件库。所有新功能的设计都必须遵循这套规范。共享链接发给大家了。"),
    ("数据分析E", 1645, 1670, "竞品动态更新：竞品X上周上线了AI会议纪要功能，用户增长了20%。竞品Y的定价页面改版后转化率提升了12%。这些数据支持我们做AI功能和定价优化的决策。"),
    ("主持人A", 1670, 1700, "好的，Q3计划基本确认。接下来讨论团队建设和招聘需求。目前团队最大的瓶颈是什么？"),
    ("产品经理B", 1700, 1720, "最大的瓶颈是后端开发资源。架构重构、支付改造、AI功能三线并行，现有人力不够。建议至少再招2个高级后端工程师。"),
    ("技术负责人C", 1720, 1745, "同意。另外还需要一个DevOps工程师，目前部署流程全是手动的，每次发版需要2小时。CI/CD自动化后可以缩短到15分钟。招聘渠道建议用Boss直聘和脉脉。"),
    ("设计师D", 1745, 1765, "设计团队也需要扩充。目前我一个人做所有的UI/UX，效率很低。建议招一个视觉设计师负责营销素材，我专注产品设计。"),
    ("数据分析E", 1765, 1785, "数据团队也需要一个数据工程师，专门负责数据管道和ETL。目前数据分析的80%时间花在数据清洗上，只有20%做真正的分析。"),
    ("主持人A", 1785, 1810, "招聘需求我汇总一下：后端2人、DevOps 1人、视觉设计1人、数据工程1人，共5个HC。我去找VP审批。"),
    ("主持人A", 1810, 1835, "还有其他需要讨论的吗？"),
    ("产品经理B", 1835, 1860, "补充一点：竞品分析发现，市场上开始出现无代码会议工具的趋势。虽然目前功能简单，但增长很快。我们需要关注这个方向，可能在Q4考虑做一个轻量版。"),
    ("技术负责人C", 1860, 1880, "无代码方向的技术可行性很高。我们可以把核心功能做成组件化，让用户通过拖拽自定义会议模板。不过这需要把业务逻辑层做得足够灵活。"),
    ("数据分析E", 1880, 1900, "无代码工具的用户画像和我们不同，主要是非技术用户。如果要进入这个市场，需要重新做用户调研和定价策略。建议先做市场验证再投入开发。"),
    ("主持人A", 1900, 1920, "好的，无代码方向列入Q4观察清单。今天的会议内容非常充实，总结一下三个核心决策。"),
    ("主持人A", 1920, 1950, "第一，Q3聚焦留存率优化，目标7日留存从18%提升到25%。第二，分层定价和架构重构并行推进。第三，Q4重点是AI功能，国际化做技术储备。"),
    ("主持人A", 1950, 1970, "行动项汇总：B出AI功能PRD和定价方案（截止2周后），C出架构重构方案和合规文档，D出onboarding和定价页设计稿，E出监控大盘和AB测试计划。"),
    ("主持人A", 1970, 1985, "下次会议时间：下周三下午2点。请各组提前准备好进展汇报。散会，感谢大家。"),
]


# ── Helper functions ──────────────────────────────────────────────────────

def count_approx_tokens(text: str) -> int:
    """Rough token count estimate (CJK-heavy text)."""
    cjk = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    other = len(text) - cjk
    return int(cjk * 1.5 + other * 0.3)


def format_segments(segments, label=""):
    """Format segments into compact format (no speaker labels, compressed timestamps).

    Matches session.py's _format_segment() output.
    """
    lines = []
    for speaker, start, end, text in segments:
        s = int(start) if start == int(start) else start
        e = int(end) if end == int(end) else end
        lines.append(f"[{s}-{e}] {text}")
    return "\n".join(lines)


def generate_summary(client: httpx.Client, base_url: str, model: str, transcript: str) -> str:
    """Generate a meeting summary using the LLM (simulates incremental summary).

    Uses the same specialized meeting prompt as session.py to ensure
    the summary retains specific numbers, speaker attributions, and details.
    """
    system = (
        "你是一位专业的会议记录员，擅长从会议转录中提取结构化摘要。"
        "你的摘要必须保留所有具体数据、数字指标、说话人归属、分歧观点和技术细节。"
        "不要泛泛概括，要保留能回答具体问题的关键信息。"
    )
    user = (
        "请根据以下会议转录生成结构化摘要，严格遵循以下格式：\n\n"
        "## 会议主题\n（一句话概括）\n\n"
        "## 关键数据与指标\n（列出所有提到的具体数字、百分比、金额、时间等）\n\n"
        "## 议题与解决方案\n（每个议题必须包含：问题描述→具体方案→负责人，保持问题和方案的对应关系）\n\n"
        "## 分歧与争论\n（如有不同意见，列出双方观点和各自论据）\n\n"
        "## 决策与共识\n（明确列出达成的决定及依据）\n\n"
        "## 行动项\n（格式：[负责人] 任务内容 — 截止时间）\n\n"
        "## 待跟进事项\n（未解决的问题和后续计划）\n\n"
        "重要规则：\n"
        "- 必须保留原始发言中的具体数字和数据\n"
        "- 每个议题的问题和解决方案必须配对出现，不要只写问题不写方案\n"
        "- 保留说话人（如A、B、C）的具体观点归属\n\n"
        f"会议转录：\n{transcript}"
    )
    resp = client.post(
        f"{base_url}/v1/chat/completions",
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": 1024,
            "temperature": 0.2,
            "stream": False,
        },
        timeout=120.0,
    )
    return resp.json()["choices"][0]["message"]["content"]


def build_old_context(full_transcript: str) -> str:
    """OLD mode: full transcript (current behavior)."""
    return f"【会议转录】\n{full_transcript}"


def build_new_context(recent_transcript: str, summary: str, window_min: int) -> str:
    """NEW mode: summary + recent window (progressive context)."""
    parts = []
    if recent_transcript:
        parts.append(f"【最近转录（近{window_min}分钟）】\n{recent_transcript}")
    if summary:
        parts.append(f"【会议摘要】\n{summary}")
    return "\n\n".join(parts)


def clear_kv_cache(base_url: str):
    """Attempt to clear the KV cache on llama.cpp server to ensure cold-start TTFT."""
    try:
        import httpx
        with httpx.Client(timeout=5.0) as c:
            # llama.cpp /props endpoint can sometimes trigger a health reset
            # The most reliable way: send a trivial request with slot override
            # Alternatively, use POST /completion with "cache_prompt": false
            c.post(f"{base_url}/v1/chat/completions",
                   json={"model": "", "messages": [{"role": "user", "content": "x"}],
                         "max_tokens": 1, "temperature": 0.0},
                   timeout=5.0)
    except Exception:
        pass


def measure_stream(client: httpx.Client, base_url: str, model: str,
                   context: str, question: str, clear_cache: bool = True) -> dict:
    """Send a chat request with streaming and measure TTFT + TPS.

    clear_cache: if True, sends a dummy request before the real one to
                 flush the KV cache, ensuring a cold-start measurement.
    """
    if clear_cache:
        clear_kv_cache(base_url)
        time.sleep(0.3)  # Brief pause to let cache clear

    full_prompt = (
        "你是一位会议助手。请仅根据提供的会议内容回答问题；"
        "如果内容中没有相关信息，请明确说明。\n\n"
        f"会议内容：\n{context}\n\n"
        f"用户问题：{question}\n\n"
        "请简洁准确地回答。如果相关信息不在会议内容中，请说明。"
    )

    t_start = time.perf_counter()
    first_token_time = None
    token_count = 0
    full_text = []

    with client.stream(
        "POST",
        f"{base_url}/v1/chat/completions",
        json={
            "model": model,
            "messages": [{"role": "user", "content": full_prompt}],
            "max_tokens": 256,
            "temperature": 0.3,
            "stream": True,
        },
        timeout=120.0,
    ) as resp:
        for line in resp.iter_lines():
            if not line.startswith("data: "):
                continue
            data = line[6:]
            if data.strip() == "[DONE]":
                break
            chunk = json.loads(data)
            delta = chunk.get("choices", [{}])[0].get("delta", {})
            content = delta.get("content", "")
            if content:
                if first_token_time is None:
                    first_token_time = time.perf_counter()
                token_count += 1
                full_text.append(content)

    t_end = time.perf_counter()
    total = t_end - t_start
    ttft = (first_token_time - t_start) if first_token_time else None
    gen_time = (t_end - first_token_time) if first_token_time else 0
    tps = token_count / gen_time if gen_time > 0 else 0

    return {
        "ttft_ms": round(ttft * 1000, 1) if ttft else None,
        "output_tokens": token_count,
        "tps": round(tps, 1),
        "total_s": round(total, 2),
        "answer": "".join(full_text)[:200],
    }


# ── Main ──────────────────────────────────────────────────────────────────

QUESTIONS = [
    "会议讨论了哪些产品方向？",
    "后端架构需要做什么改动？",
    "移动端转化率的问题如何解决？",
    "主持人最终决定了什么优先级？",
    "技术负责人和产品经理有什么分歧？",
]


def main():
    parser = argparse.ArgumentParser(description="Compare OLD vs NEW context building")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--model", default="qwen3.5:9b")
    parser.add_argument("--window-minutes", type=int, default=5,
                        help="Recent window size in minutes for NEW mode")
    args = parser.parse_args()

    client = httpx.Client()

    # ── 1. Build transcript ────────────────────────────────────────────
    full_transcript = format_segments(TRANSCRIPT_SEGMENTS)
    full_token_est = count_approx_tokens(full_transcript)

    # Split: first 80% = "already summarized", last 20% = "recent window"
    split_idx = int(len(TRANSCRIPT_SEGMENTS) * 0.8)
    old_segments = TRANSCRIPT_SEGMENTS[:split_idx]
    recent_segments = TRANSCRIPT_SEGMENTS[split_idx:]

    old_transcript = format_segments(old_segments)
    recent_transcript = format_segments(recent_segments)
    recent_token_est = count_approx_tokens(recent_transcript)

    print("=" * 70)
    print("       Context Compression Comparison Benchmark")
    print("=" * 70)
    print(f"Model:          {args.model}")
    print(f"Server:         {args.base_url}")
    print(f"Full transcript: {len(TRANSCRIPT_SEGMENTS)} segments, ~{full_token_est} tokens")
    print(f"Recent window:   last {len(recent_segments)} segments (~{args.window_minutes} min), ~{recent_token_est} tokens")
    print(f"Questions:       {len(QUESTIONS)}")
    print()

    # ── 2. Generate summary ────────────────────────────────────────────
    print("Generating summary of older segments...")
    try:
        summary = generate_summary(client, args.base_url, args.model, old_transcript)
        summary_tokens = count_approx_tokens(summary)
        print(f"Summary generated: ~{summary_tokens} tokens")
        print(f"Summary preview: {summary[:120]}...")
    except Exception as e:
        print(f"ERROR: Summary generation failed: {e}")
        print("Falling back to manual summary placeholder.")
        summary = "（摘要生成失败，请检查服务器状态）"
        summary_tokens = count_approx_tokens(summary)
    print()

    # ── 3. Warmup ──────────────────────────────────────────────────────
    print("Warming up LLM server...")
    try:
        client.post(
            f"{args.base_url}/v1/chat/completions",
            json={"model": args.model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1},
            timeout=30.0,
        )
        print("Warmup done.")
    except Exception as e:
        print(f"Warmup failed: {e}")
    print()

    # ── 4. Run comparisons ─────────────────────────────────────────────
    old_ctx = build_old_context(full_transcript)
    new_ctx = build_new_context(recent_transcript, summary, args.window_minutes)
    old_ctx_tokens = count_approx_tokens(old_ctx)
    new_ctx_tokens = count_approx_tokens(new_ctx)

    results = []

    for i, question in enumerate(QUESTIONS, 1):
        print(f"{'─' * 70}")
        print(f"Q{i}: \"{question}\"")
        print(f"{'─' * 70}")

        # OLD mode
        old_result = measure_stream(client, args.base_url, args.model,
                                    old_ctx, question, clear_cache=True)
        print(f"  OLD  ctx≈{old_ctx_tokens:>6} tok  TTFT={old_result['ttft_ms']:>8.1f}ms  "
              f"TPS={old_result['tps']:>5.1f}  answer: {old_result['answer'][:60]}...")

        # NEW mode
        new_result = measure_stream(client, args.base_url, args.model,
                                    new_ctx, question, clear_cache=True)
        print(f"  NEW  ctx≈{new_ctx_tokens:>6} tok  TTFT={new_result['ttft_ms']:>8.1f}ms  "
              f"TPS={new_result['tps']:>5.1f}  answer: {new_result['answer'][:60]}...")

        ttft_pct = ((new_result["ttft_ms"] - old_result["ttft_ms"]) / old_result["ttft_ms"] * 100
                     if old_result["ttft_ms"] else 0)
        print(f"  Δ TTFT: {ttft_pct:+.0f}%  |  Context tokens: {old_ctx_tokens} → {new_ctx_tokens} "
              f"({(new_ctx_tokens - old_ctx_tokens) / old_ctx_tokens * 100:+.0f}%)")
        print()

        results.append({
            "question": question,
            "old": old_result,
            "new": new_result,
        })

    # ── 5. Summary table ───────────────────────────────────────────────
    print("=" * 70)
    print("                       SUMMARY")
    print("=" * 70)

    old_ttfts = [r["old"]["ttft_ms"] for r in results if r["old"]["ttft_ms"]]
    new_ttfts = [r["new"]["ttft_ms"] for r in results if r["new"]["ttft_ms"]]
    old_tps   = [r["old"]["tps"]     for r in results if r["old"]["tps"] > 0]
    new_tps   = [r["new"]["tps"]     for r in results if r["new"]["tps"] > 0]

    from statistics import mean
    avg_old_ttft = mean(old_ttfts) if old_ttfts else 0
    avg_new_ttft = mean(new_ttfts) if new_ttfts else 0
    avg_old_tps  = mean(old_tps)   if old_tps   else 0
    avg_new_tps  = mean(new_tps)   if new_tps   else 0
    ttft_reduction = ((avg_new_ttft - avg_old_ttft) / avg_old_ttft * 100) if avg_old_ttft else 0

    print(f"  OLD: ctx≈{old_ctx_tokens} tokens  avg TTFT={avg_old_ttft:.0f}ms  avg TPS={avg_old_tps:.1f}")
    print(f"  NEW: ctx≈{new_ctx_tokens} tokens  avg TTFT={avg_new_ttft:.0f}ms  avg TPS={avg_new_tps:.1f}")
    print(f"  Context reduction: {(new_ctx_tokens - old_ctx_tokens) / old_ctx_tokens * 100:+.0f}%")
    print(f"  TTFT reduction:    {ttft_reduction:+.0f}%")
    print("=" * 70)

    client.close()


if __name__ == "__main__":
    main()
