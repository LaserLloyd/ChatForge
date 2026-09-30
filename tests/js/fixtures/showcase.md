## A small table

| Runtime | Device | First load | Cached load |
|---|:---:|---:|---:|
| OVMS 2026.4 | NPU | ~6 min | ~10 s |
| MiniMax | cloud | n/a | n/a |

```python
def fib(n):
    a, b = 0, 1
    for _ in range(n):
        a, b = b, a + b
    return a
```

Footnotes work.[^1] So do [links](https://example.com/docs).

> [!WARNING]
> Compiled graphs are cached per model.

[^1]: The footnote text.
