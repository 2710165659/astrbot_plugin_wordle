# astrbot_plugin_wordle

AstrBot Wordle 插件。使用 `/wordle` 开始游戏，机器人会在当前群聊或私聊里接管对应位数的纯英文消息，并返回经典 Wordle 风格棋盘图片。

## 用法

```text
/wordle
/wordle <1-10>
/wordle stop
```

- `/wordle` 默认开始 5 位单词游戏
- `/wordle <1-10>` 指定单词长度
- `/wordle stop` 结束当前群聊或私聊中的游戏

## 行为说明

- 每个群聊或私聊各自维护一局游戏
- 游戏开始后，会接管当前会话里对应位数的纯英文消息
- 验词使用插件内置的完整英文单词表 `data/words_by_length.json`
- 出题使用插件内置的 CET4 + CET6 词汇表 `data/cet_answers_by_length.json`
- 当前 CET 出题池覆盖 1 到 10 位单词

## 词库来源

- 验词词库来源：`dwyl/english-words`
  <https://github.com/dwyl/english-words>
- 原始公开英文单词表：
  <https://raw.githubusercontent.com/dwyl/english-words/master/words_alpha.txt>
- 四六级出题词库来源：`KyleBing/english-vocabulary`
  <https://github.com/KyleBing/english-vocabulary>

其中：

- `data/words_by_length.json` 由公开英文单词表整理生成，用于校验用户输入单词是否合法
- `data/cet_answers_by_length.json` 由 CET4 + CET6 词表整理生成，并与公开英文单词表取交集后作为出题池
