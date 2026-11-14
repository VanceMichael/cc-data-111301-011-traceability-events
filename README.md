# EPCIS 追溯事件的对象集合与纠错声明资料工具

这个 Python 项目用于业务来源资料的检查、原始字节归档、正式事件接收与对象
历程查询。`traceability_events/core.py` 处理单份资料预览，`archive.py` 保存
原文件；`parsing.py` 把 EPCIS 2.0 JSON 子集规范化，`store.py` 负责持久化
接收、重交与冲突，`history.py` 提供对象历程查询。`fixtures/` 保存本地业务
演练资料，`tests/` 检查全部行为。

## 环境

Python 3.11.2。依赖及其间接依赖固定在 `requirements.txt`；空清单表示只使用标准库。安装只在环境准备阶段执行：

```sh
python3 -m pip install -r requirements.txt
```

## 运行

```sh
python3 -m traceability_events inspect fixtures/events.json
python3 -m traceability_events capture fixtures/events.json --archive .work/raw
python3 -m traceability_events receive fixtures/events.json --store .work/store --archive .work/raw
python3 -m traceability_events history urn:epc:id:sgtin:0614141.107346.1 --store .work/store
```

归档返回 SHA-256、字节数及原文件名；文件名不参与内容身份。预览只说明单份
来源的基本结构，不代表关联资料已经齐全。输入不可识别时命令失败，不产生空
的成功结果。

## 正式接收语义

* 事件时间、记录时间与时区偏移分别保存；时间同时保留原文与 UTC 瞬时值，
  偏移与时间自带时区不一致时拒绝该事件。
* 对象集合不摊平：`epcList`/`quantityList`（对象）、`parentID`/`childEPCs`
  （父容器与子级）、`inputEPCList`/`outputEPCList` 及各自数量列表保持独立
  角色；数量对象保留 `epcClass`、`quantity`、`uom`，不冒充单件货号。
* `ADD`、`DELETE`、`OBSERVE` 分别表达关系建立、关系解除与仅观察现状；
  TransformationEvent 无 action，输入/输出即其事实。查询中给出每条关系的
  上游声明含义，缺少 ADD 前史只标记缺口，绝不合成入库事件。
* 批量接收允许单事件失败：回执逐条给出 accepted / redelivery /
  supplemented / conflict / rejected；只有落盘成功的事件才对查询可见，
  被拒事件返回非零退出码且不进入事件库。
* 重交与冲突：
  * 有 `eventID`：身份为 `id-` + sha256(eventID)。同内容重交幂等；仅新增
    `errorDeclaration` 计为补充并追加版本；其他任何内容差异保留为冲突，
    原始事件与冲突副本都不删除。
  * 无 `eventID`：身份为 `ch-` + 规范化事件原文的 SHA-256。同内容重交稳定
    命中同一记录，内容变化即成为新身份；不使用随机 UUID。
* `errorDeclaration` 是事件上的补充而不是删除事件：原文与后到的声明各自
  留痕；`correctiveEventIDs` 指向尚未收到的事件时，历程中列为未解决引用，
  纠正事件补交后实时转为已解决，原始证据不改写。

## 历程查询

按 EPC（或数量对象的 epcClass）返回它在所有来源事件中作为对象、父/子容器、
转换输入/输出出现的记录，含 action 的关系含义、容器视图、前史缺口、未解决
纠错引用与冲突清单。查询实时读取事件库，补交事件后立即反映。

## 开发检查

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q traceability_events tests
```

## 本地演示

```sh
python3 -m traceability_events demo
```

演示实际读取来源资料，覆盖原始归档、首次接收、同内容重交、纠错声明补交、
纠正事件补交前后未解决引用的变化，使用临时目录并自动清理。所有示例身份与
业务数值均为虚构，不需要外部账号。
