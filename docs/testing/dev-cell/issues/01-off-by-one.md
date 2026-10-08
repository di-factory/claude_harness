Page 1 skips the first records
`page(items, 1)` should return the first `size` items, but it starts at the second page.

To reproduce:

```python
from pagination import page
page(list(range(25)), 1)   # returns [10, ..., 19]; expected [0, ..., 9]
```

The last page is also wrong: `page(list(range(25)), 3)` returns `[]` instead of `[20, ..., 24]`.
