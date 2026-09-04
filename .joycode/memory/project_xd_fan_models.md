---
name: XD/京造风扇型号与协议映射
description: XD-2038FA/2988FA/KF-2988A 共用同一 BLE 控制组件与协议，控制包字节布局
type: project
---

京造/西点智能风扇的三个型号 XD-2038FA、XD-2988FA、KF-2988A 在原微信小程序中**复用同一个 XD-2988FA 控制组件与同一套 BLE 协议**。用户实际设备是 XD-2038FA（广播 manufacturer data 以 `0301F008...` 开头，小程序 app-service.js 16108 行 code→编号映射：2038FA=8、KF-2988A=9，走同一显示分支）。

**Why:** 用户一度以为设备是 2988FA 后纠正为 2038FA，担心协议不同；经反编译确认二者同组件同协议，之前基于 2988FA 的协议分析仍然有效。

**How to apply:** 涉及本集成协议改动时，无需区分这三个型号；三者控制包一致。真正差异仅在广播识别码。

**控制包（写入，15 字节）**：`AA 55 10 00 0A` + 10 数据字节，0xFF=不改变。
- byte5 power(0x01关/0x02开)、byte6 gear(1..12)、byte7 LR左右摆(0关/1=30/2=60/3=90/4=120°，app.oscillate 开时写5)、byte8 UD上下摆(0/1=30/2=60/3=120°，无90°)、byte9 manual手动方向(1上2下3左4右)、byte10 mode(1睡眠/3自选/5暴风)、byte11 timing(0取消/1..12小时)、byte12 light、byte13 voice、byte14 trumpet(均0x01关/0x02开)。
- notify 回包同布局，255=保持，开关字段值==2 为开。

**HA 集成实体映射**：fan(power/speed/oscillate/preset_mode) + switch(light/voice/trumpet) + select(lr_swing/ud_swing/timing) + button(manual_up/down/left/right)。

**命名约定**：目录/DOMAIN 用 jz_fan，但代码内品牌标识保持原始 XD（类名 XDFan* / 日志 "XD fan" / manufacturer "XD"），切勿全局替换成 JZ。