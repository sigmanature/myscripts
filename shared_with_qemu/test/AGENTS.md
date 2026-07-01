# TESTING_GUIDE.md

> 目标：测试框架**简单、可复现、可维护**。默认不做过度抽象、不做参数膨胀，优先把 case 写薄，把通用逻辑沉到 utils。

---

## 0. 项目结构（硬性）

必须分为 **utils 文件夹** + **case 文件夹**：

```
tests/
  case/
    test_xxx.py
    test_yyy.py
  utils/
    __init__.py
    *.py
```

- `tests/case/`：只放测试用例（case）
- `tests/utils/`：只放通用函数/通用模块（utils）

---

## 1. case 规则（硬性）

1) **文件名必须带 `test` 后缀**（推荐 `test_*.py`）  
2) **单文件不超过 200 行**  
3) **逻辑越薄越好**：一个 case 尽量只测一件事  
4) **偏好多文件分开测**：不要一个文件塞多个测例（组合场景就拆多个 case）

> 超过 200 行：立刻拆分或上提 utils（见第 2 条）

---

## 2. utils 规则（硬性）

1) **通用函数全部放 `tests/utils/`**  
2) 今后写 case 的过程中：
   - case 里出现可复用逻辑 → **立即上提到 utils**
   - case 文件接近/超过 200 行 → **把大段逻辑抽成 utils**

> 允许 case 调 utils，不允许反向依赖（utils 里不要 import case）

---

## 3. 参数规则（硬性）

- **禁止 argparse 膨胀**
- 用户没需求就**写死常量**
- 能简单就简单：  
  - 能不抽象就不抽象  
  - 能不防御健壮就不防御健壮（不需要上来就考虑“很通用”）
- 日志可以适度加，但要克制（见第 7 条）

> 原则：**不必要绝对不加参数**

---

## 4. 可复现（强烈建议）

- case 需要随机时：**固定 seed（写死常量）**
- case 顶部必须有一个 `CONFIG` 常量区（不搞配置系统）：

示例：
```python
# === CONFIG (edit here) ===
GROUPS = 200
IMAGE_SIZE = 512 * 1024 * 1024
FILE_SIZE  = 8 * 1024 * 1024
SLEEP_AFTER_FRONT = 10
SEED = 12345
```

---

## 5. 自包含（强烈建议）

- case 尽量**不依赖外部状态**（已有挂载点/已有文件/已有路径）
- 推荐：case 自己创建 workdir、镜像、目标文件、目录结构
- 所有临时产物放到一个 workdir 下，避免污染系统：

```
work/<case_name>/
  image.img
  mnt/
  logs/
```

---

## 6. prepare/cleanup（强烈建议）

case 建议固定形状：

- `prepare()`：准备目录/镜像/挂载/目标文件
- `run()`：只做测试动作（group loop）
- `cleanup()`：卸载/detach loop/删临时文件

要求：
- **准备逻辑必须封装**：不要散落在 group loop 里
- **默认强清理**：不留垃圾（mountpoint、loopdev、临时目录）

---

## 7. 日志（适度）

- 每个 group 打一行（足够复盘）：
  - group id、耗时、关键计数器（比如 churn created/deleted、gc pulse ok）
- 不要全程 debug spam
- 推荐统一前缀格式：
  - `[group N] ...`
  - `[prepare] ...`
  - `[cleanup] ...`

---

## 8. case 隔离（建议）

- 一个 case = 一个场景 = 一个文件  
- 不要在一个 case 里同时验证多种无关行为  
- 组合场景用多个 test 文件表达差异

---

## 9. 性能/压力用例上限（建议）

- 允许 `while True`，但默认建议写死一个合理轮数（例如 200/1000）避免跑飞
- 后台线程（churn / gc pulse）要能一行关掉（写死常量即可）：

```python
ENABLE_CHURN = True
ENABLE_GC_PULSE = True
```

---

## 10. 依赖（建议）

- 优先标准库（os/subprocess/threading/time）
- 非必要不引第三方库

---

## 11. 创建测试文件（硬性，禁止违反）

**绝对禁止** 用 `open(O_CREAT)` 直接创建后立刻 `pwrite`。

原因：F2FS 新建 inode 时会设置 `FI_INLINE_DATA`，`f2fs_inode_may_use_large_folio()` 返回 false，
导致 `mapping_set_folio_min_order` 不被调用，large-folio write 路径完全失效，测试等于白跑。

**必须使用 `utils/io.py` 中的 `create_and_fill_file(path, size, config)`**，该函数强制执行以下三步协议：

1. `open(O_CREAT|O_WRONLY|O_TRUNC)` + `ftruncate(size)` + `close` → 清除 `FI_INLINE_DATA`
2. `drop_caches(3)` → 将 inode 从 inode cache 驱逐；**不能省略**：dentry cache 命中会绕过 `f2fs_iget`，导致 `mapping_set_folio_min_order` 不被调用
3. 重新 `open(O_RDWR)` → cache miss 强制走 `f2fs_iget`，`inode_may_use_large_folio()` 返回 true，large folio 生效，然后 `pwrite` + `fsync`

三步缺一不可，不能合并为一个 fd，不能省略 drop_caches。

```python
# 正确
create_and_fill_file(FILE_PATH, FILE_SIZE, BASELINE)

# 错误 — 禁止
fd = open_rw(FILE_PATH, create=True)
pwrite_pattern_config(fd, 0, FILE_SIZE, BASELINE)  # inode 仍是 inline，large folio 不生效
```

---

## PR 门禁（强制执行）

- [ ] 新增/修改的 case 是否在 `case/` 且文件名含 `test`？
- [ ] 单文件是否 ≤ 200 行？（超过就拆/上提 utils）
- [ ] 通用逻辑是否已经上提到 `utils/`？
- [ ] 是否引入了不必要的参数/argparse？
- [ ] 是否具备基本可复现性（固定 seed / 常量区）？
- [ ] 是否具备基本清理能力（cleanup 不留垃圾）？
