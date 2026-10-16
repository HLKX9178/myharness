"""`python -m myharness` 的入口。

`myharness.config` 在 import 期就要读取模型 ID 与 API 凭据，而首次部署时
最容易缺的正是这几项。把 traceback 甩给刚克隆完仓库的人等于什么都没说，
所以这里把配置错误渲染成一条提示。

判断用类名而不是 `import ConfigError`：配置错误是在 config 模块**执行过程中**
抛出的，此时该模块尚未完成初始化、也就拿不到它的属性。
"""

import sys


def _run() -> None:
    # 延迟到函数内，好让 config 的启动期校验落在下面的 try 里
    from myharness.cli import main

    main()


if __name__ == "__main__":
    try:
        _run()
    except Exception as exc:
        if type(exc).__name__ != "ConfigError":
            raise
        print(f"\n[配置错误] {exc}\n", file=sys.stderr)
        sys.exit(2)
