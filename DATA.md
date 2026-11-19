# 资料约定

原始资料均为本地合成。CSV 使用 UTF-8，首行是字段名。

代办数量为整数份；价格为每份的分，不含税率换算。`vouchers` 按票及服务给出可核单位数。`deliveries` 为确认交付，`relations` 的 verified/possible/excluded 分别为证实、可能和排除的票据对应，最多支持 max_units 份；`recorded` 是到达时间。`voids.effective` 生效后不得继续用票。`claims` 是供应方逐行申报，delivery_hint 空值代表未指明，分行或重复申报不增加实际交付。`payments` 为原结算账，`allocation` 表示当时账上归给哪张票，不是最新对应关系。`corrections` 可更正交付数量、关系或供应方，supersedes 指被更正的事实 ID。同一票服务共用的额度不能在几份结算单分别使用。`terms` 界定可作差额申请的业务范围；跨供应方无互抵资格，需要人工解决。
`transmissions` 是此前人工逐行报送的记录，不保证当时证据无误；batch 标识报送单，行 ID 在各批之间唯一。`receipts` 是财务的回执流水，同一个 id 可以在不同 recorded 时间重复到达，不因此增加支付。received 对应整个批次，paid/rejected 对应完整的一条报送行。回执支付即传送行的份额与单价，不在 `payments` 中重复登记；历史 `payments` 为另一套已入账单据。
