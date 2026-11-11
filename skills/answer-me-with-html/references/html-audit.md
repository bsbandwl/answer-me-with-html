# 可选的 HTML 交付检查

正文与排版仍只使用 `am render` / `am patch`。此工具从 WHW 抽取检查能力，不生成页面、不要求模型编写 CSS，不替代 am.mjs，也不建立第二套默认输出流程。维护源在 whw/scripts/html_audit.py；当前本机副本在本技能 scripts/html_audit.py。

## 命令

```text
python scripts/html_audit.py static report.html
python scripts/html_audit.py static report.html --output static-new.json
python scripts/html_audit.py browser report.html --artifacts audit-new --width 390 --width 1280 --print-pdf
```

相对脚本路径以本技能目录为基准；从项目目录运行时用脚本绝对路径。Windows 使用已经确认的实际 Python，遵循 win-dev。static 仅依赖标准库；browser 复用已安装的 Python Playwright 和本机 Edge/Chrome，或显式 `--browser-exe PATH`。不安装依赖或下载浏览器，显式路径失败不偷偷回退。

## static

检查活动资源属性、CSS url/import 的非内嵌引用、重复 ID、坏 fragment 和可见文本中的未编译公式。普通 HTTP 参考链接与本地截图的导航链接允许；嵌入式 data 资源、AM 内联脚本和 CSP 不因存在本身被判错。公式检查排除 script/style/pre/code/textarea。

这是 UTF-8 HTML/CSS 启发式盘点，不是完整的 CSS/JS 解释器或安全分析，也不证明运行时没有网络请求。原始文本中的单个美元符号不作为未编译公式判据。

## browser

在隔离、非持久浏览器上下文中检查初始加载。默认宽度390/1280/1600；`--width`可重复指定，重复宽度本身会被拒绝。阻断并记录非内嵌资源、网络及 WebSocket 尝试，检查 JS 异常、页面横向溢出与 SVG 标签越界/重叠。

WebSocket 路由不建立服务器连接，本地端随 context 关闭而释放。Worker、Service Worker、WebTransport 和 WebRTC 因无法完整审计而阻断并报告；不把此模式当任意网页的兼容浏览器或操作系统安全沙箱。

保存 viewport-WIDTH.png 和 browser.json；`--print-pdf`另保存末个视口的 print.pdf。仅检查初始 load、字体就绪和短观察窗口，不覆盖任意交互、延迟网络、一般遮挡、语义或打印分页质量。SVG 几何启发式可能对旋转或有意重叠误报，需要人工判断。

## 输出与结论

只读原件并记录 SHA256；结束时再次核对原件身份。报告文件必须是新文件，制品目录必须是新目录；拒绝覆盖已有空目录、报告或原件。失败可能留下本次创建的部分制品与失败报告，重试使用新目录。

stdout 为 JSON；退出0只表示已执行检查未发现问题，退出1表示发现问题或无法检查，参数错误退出2。visual_review 与 interaction_review 固定为 not_performed，不能把检查通过说成视觉、语义、分页或实机验收通过。正式离线交付时按需使用，不给普通回答新增强制前置门禁。

已用真实 am 生成的流程图/公式页做静态、390/1280视口和打印烟测；已用外链、坏锚点、重复ID、未编译公式、溢出及网络尝试做反例。初次集成发现的PDF参数不兼容和同步WebSocket关闭死锁已修复；不以这些合成检查声称任意页面均可离线或排版正确。
