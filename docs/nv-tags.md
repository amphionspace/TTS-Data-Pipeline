# NV 标签对齐（nv-canonical-v1）

五个副语言数据集的文本统一为 NVSpeech（Emilia-NV）风格的 `[Name]` 标签。基础数据保留上游原文；
统一文本是 text revision annotation `nv_tag_text`，由 selection 采用为 selected_text，dots_vae 特征直接携带该文本。
实现：`src/tts_data_pipeline/annotations/nv_tag_text/mapping.py`；完整映射写入 annotation profile。

## 标签集合（26 个）

- NVSpeech 18 个：`[Breathing] [Laughter] [Cough] [Sigh] [Confirmation-en] [Question-en] [Question-ah] [Question-oh]
  [Surprise-ah] [Surprise-oh] [Dissatisfaction-hnn] [Uhm] [Shh] [Crying] [Surprise-wa] [Surprise-yo] [Question-ei] [Question-yi]`
- 扩展 8 个（NVSpeech 无对应的不同声音，同一格式）：`[Sniff] [Throat-clearing] [Gasp] [Yawn] [Groan] [Snore] [Sneeze] [Mouth-noise]`

原则：相似声音映射到已有 NVSpeech 标签；不同声音保留为扩展标签，即使很少；只合并几乎相同的声音。
标签放在源事件标记的原位置（NVSpeech 的标签代替该声音，不额外保留拟声字）。不因标签排除样本。
训练端分词器（LM-TTS-Training `dots-tts-soar-nv26`）以这 26 个字符串为单个 token，必须精确一致。

## 映射与计数（nv_tag_text 转换的 ok 行）

| 目标 | emilia_nv | smiip_nv | nonverbalspeech38k | nonverbaltts | disfluencyspeech |
| --- | --- | --- | --- | --- | --- |
| [Laughter] | 原生 21,007 | `<laughter>` 6,680 | `[laughing]` 6,912 | 🤣 1,278 | `<laughter>/<Laughter>` 726 |
| [Breathing] | 原生 85,560 | — | `[breath]` 2,162 | 🌬️ 6,121 | `<breathing>` 84、`<inhaling>` 19 |
| [Cough] | 原生 3,761 | `<cough>` 6,430 | `[coughing]` 5,940 | 😷 272 | `<cough>` 4 |
| [Sigh] | 原生 7,735 | — | `[sigh]` 9,783 | 😤 147 | `<sigh>` 18 |
| [Crying] | 原生 1,314 | `<crying>` 4,902 | `[crying]` 753 | — | — |
| [Dissatisfaction-hnn] | 原生 6,085 | — | — | 🐖 grunt 8 | — |
| [Sniff] | — | — | `[sniff]` 7,321 | 👃 407 | `<sniffing>` 11、`<Snorting>` 1 |
| [Throat-clearing] | — | — | `[throatclearing]` 4,909 | 🗣 214 | `<throat_clearing(s)>` 55 |
| [Gasp] | — | — | `[gasp]` 578 | — | `<gasp>` 2 |
| [Yawn] | — | — | `[yawn]` 269 | — | — |
| [Groan] | — | — | — | 😖 140 | `<groan>` 1 |
| [Snore] | — | — | `[snore]` 91 | 😴 15 | — |
| [Sneeze] | — | — | — | 🤧 18 | — |
| [Mouth-noise] | — | — | — | — | lipsmack 系 60、`<swallowing>` 6 |

emilia_nv 其余 12 个 NVSpeech 原生标签不变；其他四个数据集没有这些叹词类标签的来源。
合并：snort → [Sniff]；grunt → [Dissatisfaction-hnn]；lip smack 与 swallowing → [Mouth-noise]。gasp 单独保留，不并入 [Breathing]。

**NonverbalTTS emoji 含义为推断。** 上游未公布 emoji 与类别的对应；按 Result 文本中出现次数排序，10 个 emoji 的顺序与论文 Table 2
（Breath 3612 > Laugh 1018 > Sniff 388 > Cough 233 > Throat 203 > Sigh 145 > Groan 117 > Sneeze 16 > Snore 13 > Grunt 7）完全一致，
且每个 emoji 的字面含义相符。

## 各数据集的转换规则与异常

- **emilia_nv**：已是 NVSpeech 格式，全部 keep（174,179 行）。
- **smiip_nv**：点式标注 `<laughter>/<cough>/<crying>` 原位替换。6 处上游笔误修复并逐行记录：
  `<<crying>`（1）、`<laughter>>`/`<crying>>`（2）、缺左括号 `crying>`（1）、`<aughter>`（1）、多余 `<`（1）。
  仅有区间式标注 `<x/>…</x>`（边笑/哭边说，不是点事件）的 287 行与无标注的 3 行跳过（skipped），selection 记 1003 排除。
- **nonverbalspeech38k**：`[tag]` 替换；657 行主文本残留的 `<B>…</B>` 区间标记删除、文字保留。
- **nonverbaltts**：emoji 换成标签；35 处 emoji 被上游删除后残留的不可见变体选择符 U+FE0F 删除；空文本 5 行跳过。
- **disfluencyspeech**：Switchboard 标注还原为实际说出的词：`{C|D|E|F|A …}` 去掉括号保留词（uh/um 等填充词按词保留，不转 [Uhm]），
  `[ 原话 + 修正 ]` 两者都保留，`(( … ))` 去括号，独立的 `-`/`--` 删除；4 处不成对的 `{X`/`}` 删除。

每行 annotation 结果记录 source_tags、target_tags、extended（是否含扩展标签）与 fixes，可按原因追溯或排除。

## 发布链

1. annotation `tts-ann-nv_tag_text-20261103T171702bjt-01`（五个数据集，sample_table）。
2. selection selection-nv-v1 `tts-selection-supervised-tts-20261103T173114bjt-01`：采用上述文本；另排除 nonverbaltts 的 16 kHz（VoxCeleb）片段（3003）、
   1 秒以下片段、上游测试集（test、spk_test）与重复冲突，见 [selection 规范](data-contract/specs/12-selections.md#selection-nv-v1)。
3. dots_vae `…-dots_vae-dots-tts-soar-vae-xvec-48k-fp32-v2-20261103T173207bjt-01`（selection_branch）：text 为选用文本，
   text_kind=`nv-canonical-v1`，manifest.text 记 annotation run 与映射版本。
