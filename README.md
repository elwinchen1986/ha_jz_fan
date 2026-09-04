# 京造智能风扇 (JZ Smart Fan BLE)

Home Assistant 自定义集成，通过蓝牙 (BLE) 直接控制京造智能风扇，无需网关、无需云端，纯本地控制。

> 协议由京造风扇微信小程序逆向分析得到。

---

## 功能特性

| 功能 | 实体 | 说明 |
| --- | --- | --- |
| 开关 / 调速 | `fan` | 12 档风速，映射为 0-100% |
| 左右摆头 | `fan` | oscillate 摆动开关 |
| 预设模式 | `fan` | 睡眠 / 自定义 / 强风 |
| 指示灯 | `switch` | 面板指示灯开关 |
| 语音提示 | `switch` | 操作语音播报开关 |
| 蜂鸣器 | `switch` | 按键提示音开关 |

---

## 安装方式

### 方式一：HACS（推荐）

1. 安装 [HACS](https://hacs.xyz/)
2. HACS → 右上角三点菜单 → 自定义存储库
3. 仓库地址：`https://github.com/elwinchen1986/ha_jz_fan`，类别选择 **Integration**
4. 搜索并安装 **京造智能风扇**
5. 重启 Home Assistant

### 方式二：手动安装

```bash
# 复制到 HA 配置目录的 custom_components 下
cp -r custom_components/jz_fan /path/to/your/ha/config/custom_components/
# 重启 HA
```

---

## 配置

1. 确保风扇已开机且蓝牙在 Home Assistant 主机可及范围内
2. **设置 → 设备与服务 → 添加集成**
3. 搜索 **京造智能风扇**
4. 从扫描到的蓝牙设备列表中选择你的风扇（名称含 `BT2G` / `F008` 等的会排在前面）
5. 确认添加即可

---

## 图标说明

- 集成内的**配置流程与实体图标**由 [`icons.json`](custom_components/jz_fan/icons.json) 提供（MDI 图标），安装后即生效。
- 集成列表 / 设备页顶部的**品牌 Logo**由官方 [home-assistant/brands](https://github.com/home-assistant/brands) 仓库统一提供，需将 [`brand/icon.png`](brand/) 提交到该仓库的 `custom_integrations/jz_fan/` 目录，PR 合并后自动显示。详见 [`brand/README.md`](brand/README.md)。

---

## 要求

- Home Assistant 2024.1.0 及以上
- 主机具备可用的蓝牙适配器，并已启用 Home Assistant 的 Bluetooth 集成

---

## 常见问题

| 问题 | 解决方案 |
| --- | --- |
| 扫描不到设备 | 确认风扇已开机、距离主机较近，检查蓝牙适配器是否正常 |
| 添加后不可用 | 稍等重连，或将主机靠近风扇后重载集成 |
| 控制无响应 | 查看 HA 日志中 `jz_fan` 相关错误 |

---

## License

[MIT](LICENSE)