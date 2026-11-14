# 来源资料约定

来源为 EPCIS 2.0 JSON 业务子集；此处省略 JSON-LD 联网上下文解析，不访问远端术语服务。events 包含聚合和转换事实；declaration 在原事件上补充 errorDeclaration，correctiveEventIDs 指向纠正事件，并非把声明当成独立删除事件。示例 EPC 对象为本地虚构标识，bizStep 使用项目内短名。

## 子集字段与角色约定

* 支持 `ObjectEvent`、`AggregationEvent`、`TransformationEvent` 三类事件，
  以及事件上附加的 `errorDeclaration`。
* `eventTime`、`recordTime` 均为带时区的 ISO 8601 字符串；
  `eventTimeZoneOffset` 必须与 `eventTime` 自带偏移一致，三者分别保存。
* 各对象集合保持独立角色，不合并为同一种货号：
  * ObjectEvent：`epcList` 与 `quantityList` → 对象；
  * AggregationEvent：`parentID` → 父容器，`childEPCs` 与
    `childQuantityList` → 子级；`action` 必填（ADD/DELETE/OBSERVE）；
  * TransformationEvent：`inputEPCList`/`inputQuantityList` → 输入，
    `outputEPCList`/`outputQuantityList` → 输出，无 action。
* 数量元素要求 `epcClass` 与数值型 `quantity`，`uom` 可选。
* `errorDeclaration` 要求 `declarationTime`、`reason`，
  `correctiveEventIDs` 为事件标识数组，允许指向尚未接收的纠正事件。
* `eventID` 缺失时，接收方以规范化原文的 SHA-256 作为稳定内容身份；来源
  不应依赖接收方生成随机标识来完成重交识别。
