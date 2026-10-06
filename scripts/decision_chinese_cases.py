"""Public v2 synthetic contrasts with fictional nodes; no production memories or credentials.

Each tuple is a query and two candidates: direct evidence, then insufficient
evidence. Chinese and English versions have the same intended meaning. Gold
labels reflect whether a candidate supplies an answer, not whether it agrees
with the question (an explicit negative answer is valid evidence).
"""

PAIRS = {
    "negation": [
        (("阿宁要求周报使用什么语言？", "阿宁不是不喜欢中文；她已明确要求周报用中文。", "阿宁不是不喜欢周报，但没有说明周报使用什么语言。"),
         ("Which language did Anning require for weekly reports?", "It is not that Anning dislikes Chinese; she explicitly required weekly reports in Chinese.", "It is not that Anning dislikes weekly reports, but she has not specified their language.")),
        (("当前自动删除是否已经关闭？", "不是暂缓：自动删除已经明确关闭，当前只做预览。", "不能认为自动删除已经关闭；当前开关状态尚未核实。"),
         ("Has automatic deletion currently been disabled?", "It is not merely postponed: automatic deletion has explicitly been disabled; currently only previews run.", "One cannot assume automatic deletion has been disabled; the current switch status has not been verified.")),
        (("负责人是否批准了本轮上线？", "负责人明确拒绝了本轮上线申请。", "并非所有上线申请都会获批，本轮申请尚无结论。"),
         ("Did the owner approve this release?", "The owner explicitly rejected this release request.", "Not every release request is approved; there is no conclusion for this request yet.")),
        (("本轮检索会不会调用云端模型？", "本轮只走本地，不调用任何云端模型。", "不要默认本轮会调用云端模型；具体路由尚未确认。"),
         ("Will this retrieval call a cloud model?", "This retrieval is local only and calls no cloud model.", "Do not assume this retrieval calls a cloud model; its route has not been confirmed.")),
    ],
    "referent": [
        (("放大镜动效要求放在哪一层？", "放大镜动效必须位于最顶层；标志和外圈放在最底层。", "标志和外圈要放在最底层，在放大镜动效的下方；未指定放大镜自身的具体层级。"),
         ("Which layer is required for the magnifier animation?", "The magnifier animation must be on the topmost layer; the logo and ring go on the bottommost layer.", "The logo and ring go on the bottommost layer, below the magnifier animation; the magnifier's own exact layer is unspecified.")),
        (("小岚把哪台机器留作回滚副本？", "小岚保留了北松机和南竹机；她明确把前者留作回滚副本。", "小岚保留了北松机和南竹机；有人说留一台作回滚，但没说是哪台。"),
         ("Which machine did Xiaolan keep as the rollback copy?", "Xiaolan kept the Pinenode and Bamboonode machines; she explicitly retained the former as the rollback copy.", "Xiaolan kept the Pinenode and Bamboonode machines; someone suggested keeping one for rollback without saying which.")),
        (("小林最后修改的是哪个模块？", "检索模块和配置模块都检查了；小林最后修改的是后者。", "小林和小周检查了检索模块、配置模块；他们说它要修改，但没指明模块。"),
         ("Which module did Xiaolin modify last?", "Both retrieval and configuration modules were inspected; Xiaolin last modified the latter.", "Xiaolin and Xiaozhou inspected retrieval and configuration modules; they said it needed changes without specifying the module.")),
        (("谁负责更新模型配置？", "小林负责更新模型配置，小周负责重排参数；前者今天已完成。", "小林和小周都参与模型配置讨论，但负责人尚未分配。"),
         ("Who is responsible for updating model configuration?", "Xiaolin updates model configuration and Xiaozhou handles reranking parameters; the former finished today.", "Xiaolin and Xiaozhou both discussed model configuration, but responsibility has not been assigned.")),
    ],
    "temporal": [
        (("2026年9月10日最后确认的部署地点是哪里？", "9月1日选北松；2026年9月10日正式改为南竹，这是当天最后确认。", "2026年9月1日确认部署北松；9月10日状态没有记录。"),
         ("What deployment location was last confirmed on September 10, 2026?", "Pinenode was chosen on September 1; it was formally changed to Bamboonode on September 10, 2026, the last confirmation that day.", "Pinenode deployment was confirmed on September 1, 2026; no September 10 status was recorded.")),
        (("2026年9月1日当时图库有多少张图片？", "2026年9月1日图库有60张，9月20日扩到204张。", "只知道2026年9月20日图库有204张，9月1日数量未记录。"),
         ("How many images did the gallery have on September 1, 2026?", "The gallery had 60 images on September 1, 2026, and expanded to 204 on September 20.", "Only 204 images on September 20, 2026 are known; the September 1 count was not recorded.")),
        (("2026年10月4日生效的告警线是多少？", "9月30日告警线为5000，10月4日已改为10000并生效。", "9月30日告警线为5000；尚未检查10月4日生效值。"),
         ("What alert threshold was in effect on October 4, 2026?", "The threshold was 5000 on September 30; it changed to 10000 and took effect on October 4.", "The threshold was 5000 on September 30; the value in effect on October 4 has not been checked.")),
        (("2026年10月1日修正后，交付月份是什么？", "原定10月交付，2026年10月1日正式修正为11月。", "原定10月交付；据说10月1日有修正，但修正内容未记录。"),
         ("What is the delivery month after the October 1, 2026 correction?", "Delivery was planned for October; on October 1, 2026 it was formally corrected to November.", "Delivery was planned for October; a correction reportedly occurred on October 1, but its content was not recorded.")),
    ],
    "alias": [
        (("松鸦的生日是哪一天？", "松鸦是韩宁的别名；韩宁生日为农历正月二十九。", "松鸦和韩宁是不同的人；只记录了韩宁生日为农历正月二十九。"),
         ("When is Jay's birthday?", "Jay is Hanning's alias; Hanning's birthday is the twenty-ninth day of the first lunar month.", "Jay and Hanning are different people; only Hanning's birthday, the twenty-ninth day of the first lunar month, is recorded.")),
        (("老陈最喜欢用哪种语言写周报？", "老陈就是陈默；陈默最喜欢中文周报。", "老陈和陈默没有已确认的对应关系；只知道陈默喜欢中文周报。"),
         ("Which language does Old Chen prefer for weekly reports?", "Old Chen is Chen Mo; Chen Mo prefers Chinese weekly reports.", "There is no confirmed mapping between Old Chen and Chen Mo; only Chen Mo's preference for Chinese reports is known.")),
        (("北辰计划的最终节点在哪里？", "北辰计划在本记录中也叫PX；PX最终节点确认在南竹。", "北辰计划和PX是两个独立项目；这里只记录PX节点在南竹。"),
         ("Where is Project Polaris's final node?", "Project Polaris is also called PX in this record; PX's final node is confirmed in Bamboonode.", "Project Polaris and PX are separate projects; only PX's Bamboonode node is recorded here.")),
        (("小荷保留哪台服务器作备份？", "小荷是何宁的昵称；何宁明确保留北松服务器作备份。", "小荷是何宁的昵称，但本记录只有何宁喜欢简洁界面的偏好。"),
         ("Which server does Xiaohe keep for backup?", "Xiaohe is Hening's nickname; Hening explicitly keeps the Pinenode server for backup.", "Xiaohe is Hening's nickname, but this record contains only Hening's preference for a simple interface.")),
    ],
    "colloquial": [
        (("这回最后拍板把服务搁哪儿？", "最终拍板：服务搬南竹，北松只兜底。", "南竹服务今天有点卡；最终搬哪儿还没拍板。"),
         ("Where did we finally settle on putting the service this time?", "Final call: move the service to Bamboonode; Pinenode is only the fallback.", "The Bamboonode service is lagging today; the final destination has not been decided.")),
        (("备份翻车后靠哪个节点兜底？", "备份失败时由北松节点承担回滚恢复。", "北松节点今天运行顺畅，但没指定备份失败由谁兜底。"),
         ("Which node picks up the slack if backup fails?", "The Pinenode node handles rollback recovery if backup fails.", "The Pinenode node is running smoothly today, but no node has been assigned to handle backup failure.")),
        (("配置这事是谁来收尾？", "配置的最后验收和收尾明确交给小林。", "小林参加了配置讨论，但收尾负责人还没定。"),
         ("Who wraps up the configuration work?", "Final verification and wrap-up of configuration are explicitly assigned to Xiaolin.", "Xiaolin joined the configuration discussion, but the wrap-up owner has not been decided.")),
        (("老陈这轮是打算先本地试水还是直接上生产？", "老陈已决定先在本地试验，通过后再申请部署生产。", "老陈说不要掉链子，但没有选定本地或生产的试验顺序。"),
         ("Does Old Chen intend to test the waters locally or go straight to production this round?", "Old Chen decided to test locally first and request production deployment after passing.", "Old Chen said not to let him down but has not chosen the local-versus-production testing order.")),
    ],
    "quantity": [
        (("九点半那次测试等了多久？", "九点半那次测试耗时300毫秒，也就是0.3秒。", "九点半那次测试调用了300次，耗时没有记录。"),
         ("How long did the 9:30 test take?", "The 9:30 test took 300 milliseconds, or 0.3 seconds.", "The 9:30 test made 300 calls; its duration was not recorded.")),
        (("十个样本里答对了几个？", "十个样本错了两个，其余八个答对。", "十个样本中有两个是英文，其余中文；没有记录答对数量。"),
         ("How many of the ten samples were answered correctly?", "Two of the ten samples were wrong; the remaining eight were correct.", "Two of the ten samples were English and the rest Chinese; the correct-answer count was not recorded.")),
        (("已确认的生日是农历还是公历哪一天？", "已确认生日是农历正月二十九；公历日期每年不同。", "只知道生日在春天，农历和公历的具体日期都未确认。"),
         ("What confirmed birthday date is it, and is it lunar or Gregorian?", "The confirmed birthday is the twenty-ninth day of the first lunar month; the Gregorian date varies each year.", "Only that the birthday is in spring is known; neither a specific lunar nor Gregorian date is confirmed.")),
        (("当前已批准的内存上限是多少MB？", "当前已批准上限为1GB，按本协议1GB等于1024MB。", "探针当前占用1024MB，但已批准上限没有记录。"),
         ("What is the currently approved memory limit in MB?", "The currently approved limit is 1GB; this protocol defines 1GB as 1024MB.", "The probe currently uses 1024MB, but the approved limit was not recorded.")),
    ],
    "commitment": [
        (("已正式批准的部署地点是什么？", "负责人正式批准服务部署到南竹。", "小林建议部署南竹，但负责人尚未批准任何地点。"),
         ("What deployment location has been formally approved?", "The owner formally approved deploying the service to Bamboonode.", "Xiaolin suggested Bamboonode, but the owner has not approved any location.")),
        (("已生效的决策模型是哪个？", "已批准并启用Jev作为检索核验模型。", "Jev在讨论中得分较高，但尚未选定或启用决策模型。"),
         ("Which decision model is already active?", "Jev has been approved and enabled for retrieval evidence checks.", "Jev scored well in discussion, but no decision model has been selected or enabled.")),
        (("目前正式约定的删除前置步骤是什么？", "团队正式约定：生产删除前必须先留墓碑。", "有人建议删除前留墓碑，但团队尚未形成正式约定。"),
         ("What is the current formally agreed prerequisite for deletion?", "The team formally agreed that production deletion must first create a tombstone.", "Someone suggested tombstones before deletion, but the team has no formal agreement yet.")),
        (("当前已经上线的版本号是什么？", "当前已完成上线，服务实际运行f0.3+。", "f0.3+是下一版计划，尚未上线；本记录没写当前版本。"),
         ("What version is currently deployed?", "Deployment is complete and the service is actually running f0.3+.", "f0.3+ is planned for the next release and is not deployed; this record does not give the current version.")),
    ],
    "topic_only": [
        (("韩宁的生日是哪一天？", "韩宁已确认生日是农历正月二十九。", "测试报告声称成功命中了韩宁的生日，但没有给出日期。"),
         ("When is Hanning's birthday?", "Hanning's confirmed birthday is the twenty-ninth day of the first lunar month.", "A test report claims it retrieved Hanning's birthday successfully, but gives no date.")),
        (("模型核验的p50是多少毫秒？", "本轮模型核验p50测得450毫秒。", "本轮讨论了模型核验的p50优化，没有列出测量值。"),
         ("What is the model evidence-check p50 in milliseconds?", "This round's model evidence-check p50 measured 450 milliseconds.", "This round discussed optimizing model evidence-check p50 without listing a measured value.")),
        (("北松节点的备份目录具体是什么？", "北松节点备份目录明确为/srv/backup。", "北松节点已确认存在备份目录，但本记录没有路径。"),
         ("What is the exact backup directory on the Pinenode node?", "The Pinenode node's backup directory is explicitly /srv/backup.", "A backup directory exists on the Pinenode node, but this record gives no path.")),
        (("图库中HOT_PHOTOS数组包含几张图片？", "HOT_PHOTOS数组总共包含60张图片。", "HOT_PHOTOS数组的性能已优化，未记录其中图片数量。"),
         ("How many images does the gallery's HOT_PHOTOS array contain?", "The HOT_PHOTOS array contains 60 images in total.", "The HOT_PHOTOS array's performance was optimized; its image count was not recorded.")),
    ],
}


def cases():
    rows = []
    for category, pairs in PAIRS.items():
        for number, (zh, en) in enumerate(pairs, 1):
            for polarity, index in ((True, 1), (False, 2)):
                rows.append({"id": f"{category}-{number}-{'positive' if polarity else 'negative'}",
                             "family": f"{category}-{number}", "category": category,
                             "expected": polarity,
                             "zh": {"query": zh[0], "candidate": zh[index]},
                             "en": {"query": en[0], "candidate": en[index]}})
    return rows
