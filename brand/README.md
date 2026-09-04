# 品牌图标 (Brand Icon)

> 本目录用于存放要提交到官方 [home-assistant/brands](https://github.com/home-assistant/brands) 仓库的图标素材。
> **把图片放在这里本身不会让 HA 显示 Logo**，必须提交并合并到 brands 仓库后才会生效。

## 为什么需要这一步

Home Assistant 集成列表和设备页顶部的品牌 Logo，由前端从
`https://brands.home-assistant.io/_/<domain>/icon.png` 加载，按集成的 `domain`（这里是 `jz_fan`）匹配。
自定义集成的图标存放在 brands 仓库的 `custom_integrations/` 目录下。

## 图片规范

| 文件 | 尺寸 | 说明 |
| --- | --- | --- |
| `icon.png` | 256 × 256 | 必需，正方形，PNG，透明背景，内容居中并留白 |
| `icon@2x.png` | 512 × 512 | 推荐，高清屏用 |
| `logo.png` | 高度 128~256，宽度不限 | 可选，横向 Logo |
| `logo@2x.png` | 上面的 2 倍 | 可选 |

要求：PNG 格式、透明背景、经过压缩（可用 <https://tinypng.com>）。

## 提交步骤

1. 在本目录准备好 `icon.png`（256×256）。
2. Fork <https://github.com/home-assistant/brands>。
3. 在 fork 中创建目录 `custom_integrations/jz_fan/`，放入 `icon.png`（及可选的 `icon@2x.png` / `logo.png`）。
4. 提交 Pull Request，等待 Home Assistant 团队审核合并。
5. 合并后访问验证：`https://brands.home-assistant.io/_/jz_fan/icon.png`
6. 重启 HA、清理浏览器缓存后，集成即显示自定义 Logo。

## 参考

- 品牌规范：<https://developers.home-assistant.io/docs/creating_integration_brand/>
- 图片规范：<https://developers.home-assistant.io/docs/core/integration/brand_images/>