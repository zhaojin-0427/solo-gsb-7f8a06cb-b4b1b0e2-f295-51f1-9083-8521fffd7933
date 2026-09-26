# 电子表格公式求值 API

一个完全本地运行的电子表格公式求值服务，使用 Python + FastAPI 实现。
**不依赖任何外部服务，也不会执行输入中的代码**（公式通过手写递归下降解析器
解析为 AST 后再求值，不存在 `eval`）。

## 支持的公式

- 整数字面量（任意精度，如 `42`、`-5`）
- 单元格引用（A1 式地址，如 `A1`、`AA12`、`XFD1`）
- 一元正负号：`-A1`、`+3`
- 二元运算：加法 `+`、减法 `-`、乘法 `*`（按常规优先级，`*` 高于 `+ -`，
  左结合）
- 括号：`(A1 + 2) * 3`
- 矩形区域求和：`SUM(A1:B3)`（区域两角顺序可互换；未提供的格子按 0 计算）
- 记号之间允许空白，如 `= 1 + SUM( A1 : B2 )`

每个格子的值可以是**整数**，或以 `=` 开头的**公式字符串**。

## 错误码

| 结果       | 含义                                                                 |
| ---------- | -------------------------------------------------------------------- |
| `SYNTAX`   | 该格公式有语法错误；**传递性地引用该格的格子也返回 `SYNTAX`**        |
| `CYCLE`    | 该格处于引用环中；**传递性地引用该环的格子也返回 `CYCLE`**           |

- 错误只沿引用链传播，**不影响无关格子**。
- 若一个格子同时能到达语法错误格与环，结果为 `SYNTAX`（环只能在语法正确的
  格子之间形成）。
- 未在请求中提供的格子按 `0` 计算，不参与环检测。
- 地址键非法（如 `"not-a-cell"`）属于请求格式错误，返回 HTTP `422`。

## 一键启动（Docker Compose）

```bash
docker compose up --build
```

服务监听 <http://localhost:8000>，启动后可访问：

- 健康检查：<http://localhost:8000/healthz>
- 交互式 API 文档（Swagger UI）：<http://localhost:8000/docs>

## 调用示例

### cURL

```bash
curl -X POST http://localhost:8000/evaluate \
  -H 'Content-Type: application/json' \
  -d '{
    "cells": {
      "A1": 1, "A2": 2, "A3": 3,
      "B1": 4, "B2": 5, "B3": 6,
      "C1": "=SUM(A1:B3) + A1",
      "D1": "=D1",
      "E1": "=1+",
      "F1": "=E1",
      "G1": "=A2 * -2"
    }
  }'
```

响应（`results` 按地址字典序排列）：

```json
{
  "results": {
    "A1": 1,
    "A2": 2,
    "A3": 3,
    "B1": 4,
    "B2": 5,
    "B3": 6,
    "C1": 22,
    "D1": "CYCLE",
    "E1": "SYNTAX",
    "F1": "SYNTAX",
    "G1": -4
  }
}
```

### Python（标准库）

```python
import json
import urllib.request

payload = {
    "cells": {
        "A1": 10,
        "B1": "=A1*2",
        "C1": "=SUM(A1:B1)+1",
    }
}
req = urllib.request.Request(
    "http://localhost:8000/evaluate",
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
print(json.load(urllib.request.urlopen(req)))
# {'results': {'A1': 10, 'B1': 20, 'C1': 31}}
```

### `POST /cycle-repair`（断环方案）

同一个 `cells` 入参，返回把**最少几个公式**替换成整数 `0` 后即可消除全部
引用环的方案：

```bash
curl -X POST http://localhost:8000/cycle-repair \
  -H 'Content-Type: application/json' \
  -d '{
    "cells": {
      "A1": "=B1",
      "B1": "=A1",
      "C1": "=D1",
      "D1": "=C1",
      "E1": "=1+"
    }
  }'
```

```json
{
  "replace_with_zero": ["A1", "C1"],
  "minimum_count": 2
}
```

- 以**有效公式**对已提供格子的引用建有向边：`SUM(A1:B3)` 逐格计入；
  缺格、整数格和语法错误公式都没有出边，不会参与环。
- 替换一个公式只删除该格的出边（其余格子不变），等价于把该格从环中移除。
- 若存在多个替换数相同的方案，取**升序地址列表字典序最小**的一组。
- 本来无环时返回 `{"replace_with_zero": [], "minimum_count": 0}`。
- 有效公式超过 **16 个**时返回 HTTP `422`；其余输入校验规则与
  [`/evaluate`](#post-evaluate) 完全相同。

Python 调用示例：

```python
import json
import urllib.request

payload = {"cells": {"A1": "=B1", "B1": "=A1"}}
req = urllib.request.Request(
    "http://localhost:8000/cycle-repair",
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
print(json.load(urllib.request.urlopen(req)))
# {'replace_with_zero': ['A1'], 'minimum_count': 1}
```

### `POST /fill-formula`（相对填充公式）

把 `source` 格中的公式复制到 `destinations` 中的每个目标格，并按目标相对源的
**行列位移**平移引用：普通引用与 `SUM` 区域的两个端点都会平移；数字常量、
运算符、括号和空白原样保留，**不计算任何值**。所有目标公式都直接由请求中的
原公式生成（不会基于上一个生成结果链式生成）。

```bash
curl -X POST http://localhost:8000/fill-formula \
  -H 'Content-Type: application/json' \
  -d '{
    "cells": {
      "A1": 1, "A2": 2, "A3": 3,
      "B1": 4, "B2": 5, "B3": 6,
      "C1": "=SUM(A1:B1)+1"
    },
    "source": "C1",
    "destinations": ["C2", "C3"]
  }'
```

```json
{
  "cells": {
    "A1": 1,
    "A2": 2,
    "A3": 3,
    "B1": 4,
    "B2": 5,
    "B3": 6,
    "C1": "=SUM(A1:B1)+1",
    "C2": "=SUM(A2:B2)+1",
    "C3": "=SUM(A3:B3)+1"
  }
}
```

- 响应中的 `cells` 是**完整更新后的表**（键统一为大写规范地址、按字典序排列），
  可原样作为 `cells` 字段提交给 [`POST /evaluate`](#post-evaluate)。
- 目标格允许与已有格重名，会**覆盖**该格原有的值；目标与源相同也是合法操作。
- `destinations` 规范化后出现重复地址（如同时给 `"C2"` 和 `"c2"`）时返回
  HTTP `422`。
- 任一引用平移后越出 A1 地址范围（列 `< A` / `> ZZZ`、行 `< 1` /
  `> 1048576`），或生成的公式超过 **1000 个字符**的现有长度上限时，**整次**
  请求返回 `422`，不输出任何部分结果。
- `source` 必须存在且是**语法有效**的公式；整数格、缺失格、语法错误公式作为源
  都返回 `422`。`cells` 的输入校验（非法键、布尔、浮点、非 `=` 开头字符串）
  与 `/evaluate` 完全相同。

Python 调用示例（结果直接提交给 `/evaluate` 求值）：

```python
import json
import urllib.request

payload = {
    "cells": {"A1": 10, "A2": 20, "B1": "=A1*2"},
    "source": "B1",
    "destinations": ["B2"],
}
req = urllib.request.Request(
    "http://localhost:8000/fill-formula",
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
filled = json.load(urllib.request.urlopen(req))["cells"]
# {'A1': 10, 'A2': 20, 'B1': '=A1*2', 'B2': '=A2*2'}

req = urllib.request.Request(
    "http://localhost:8000/evaluate",
    data=json.dumps({"cells": filled}).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
print(json.load(urllib.request.urlopen(req)))
# {'results': {'A1': 10, 'A2': 20, 'B1': 20, 'B2': 40}}
```

## API 说明

### `POST /evaluate`

请求体：

```json
{ "cells": { "A1": 1, "B1": "=A1+1" } }
```

- `cells`：对象，键为 A1 式地址（大小写不敏感，输出统一为大写规范化形式），
  值为整数或以 `=` 开头的公式。
- 空请求体 / `{"cells": {}}` 合法，返回 `{"results": {}}`。

响应体：

```json
{ "results": { "A1": 1, "B1": 2 } }
```

`results` 的值为整数结果或错误码字符串 `"SYNTAX"` / `"CYCLE"`。

### `POST /cycle-repair`

请求体与 `POST /evaluate` 完全相同（同样的校验规则，非法键、布尔、浮点、
非 `=` 开头字符串都返回 `422`）。

响应体：

```json
{ "replace_with_zero": ["A1", "C1"], "minimum_count": 2 }
```

- `replace_with_zero`：建议替换为整数 `0` 的公式格地址（已规范化为大写，
  按字典序升序排列）。
- `minimum_count`：最少需要替换的公式数，即数组长度；无环时为 `0`。
- 有效公式超过 16 个时返回 HTTP `422`（最小反馈点集为 NP 难问题，
  服务端只对 16 个以内的规模给出精确解）。

### `POST /fill-formula`

请求体：

```json
{
  "cells": { "A1": 1, "B1": "=A1+1" },
  "source": "B1",
  "destinations": ["B2", "B3"]
}
```

- `cells`：与 `POST /evaluate` 相同（同样的校验规则）。
- `source`：源公式所在格地址（大小写不敏感）；该格必须持有**语法有效**的公式。
- `destinations`：目标格地址数组；按每个目标相对源的行列位移复制公式。

响应体（`cells` 为完整更新表，可直接提交给 `/evaluate`）：

```json
{
  "cells": { "A1": 1, "B1": "=A1+1", "B2": "=A2+1", "B3": "=A3+1" }
}
```

仅平移普通引用与 `SUM` 区域的两个端点；数字常量、运算符、括号与空白保持原样，
输出地址统一为大写规范形式。以下情况返回 HTTP `422`：

- 源缺失、不是公式或公式语法错误；
- `source` / 任一目标地址不是合法 A1 地址；
- 目标地址规范化后重复；
- 任一引用平移后越出 A1 范围（`A1`…`ZZZ1048576`）；
- 生成的公式超过 1000 个字符；
- `cells` 未通过与 `/evaluate` 相同的输入校验。

越界或超长时整次请求失败，**不会产生部分更新**。

## 本地开发（不使用 Docker）

需要 Python 3.11+：

```bash
python -m pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

运行测试：

```bash
python -m pip install pytest
pytest -q
```

## 实现要点

- `app/parser.py`：手写递归下降解析器，把公式解析为不可变 AST；
  整数/引用/一元/二元/括号/`SUM(矩形)` 白名单语法，其余一律语法错误。
- `app/engine.py`：
  - 逐格解析、构建“只包含已提供格子”的依赖图（缺格按 0，不入图）；
  - 迭代式三色 DFS 找出所有环上节点，再在反图上传播得到 `CYCLE` 集合；
  - 语法错误格同样在反图上传播得到 `SYNTAX` 集合（优先级高于 `CYCLE`）；
  - 健康节点用 Kahn 拓扑排序 + 迭代式 AST 后序遍历求值（支持极深依赖链，
    不受 Python 递归深度限制）；
  - `/cycle-repair` 在有效公式格诱导的有向图上，按替换数从小到大枚举
    反馈点集（`itertools.combinations` 天然按索引字典序产出，故同数时
    直接得到升序地址列表字典序最小的解），并用 Kahn 算法判定剩余图是否
    有环；输出按地址字典序排列。
- `app/main.py`：FastAPI 路由与请求校验（拒绝布尔、浮点及非 `=` 开头字符串）。
- `app/fill.py`：`/fill-formula` 相对填充。原公式先通过语法校验，再用与解析器
  一致的词法规则扫描出所有普通引用与 `SUM` 端点的文本区间；每个目标都以
  **同一份原公式文本**按行列位移替换这些区间（其余字符逐字保留），引用重写为
  大写规范地址。所有新公式先全部生成并通过越界/长度检查，再统一覆盖写入，
  因此失败时不会输出部分结果。
