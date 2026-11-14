"""运行期教法层：三套 preset 与提示成本口径。B 的 hint/dialog 可以 import 这里。

`app/tutor/` 由 A（policy.py）与 B（memory/hint/dialog.py）共用，
这个文件不 import 任何 B 的模块，避免循环依赖。
"""
