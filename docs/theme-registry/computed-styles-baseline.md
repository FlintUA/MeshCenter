# Computed-styles snapshot — Stage 0 baseline

Computed `background-color` / `color` / `border-color` / `box-shadow` for one representative element per component category, sampled from the Stage 0 fixture pages (`.theme_stage0_scratch/fixtures/fixture_{light,dark}.html`) in headless Chromium. This is the 'before' reference future stages diff against.

| Component | Selector | Theme | background-color | color | border-color | box-shadow |
|---|---|---|---|---|---|---|
| Panel (chat area) | `.chat-area` | light | `rgb(245, 247, 250)` | `rgb(16, 35, 63)` | `rgb(16, 35, 63)` | `none` |
| Panel (chat area) | `.chat-area` | dark | `rgb(24, 33, 44)` | `rgb(242, 247, 252)` | `rgb(10, 18, 28)` | `none` |
| Card (settings card) | `.settings-card--general` | light | `rgb(247, 249, 252)` | `rgb(16, 35, 63)` | `rgb(215, 224, 234)` | `none` |
| Card (settings card) | `.settings-card--general` | dark | `rgb(27, 40, 55)` | `rgb(242, 247, 252)` | `rgb(47, 62, 80)` | `none` |
| Control / input (search box) | `.search-input-wide` | light | `rgb(248, 248, 248)` | `rgb(51, 51, 51)` | `rgb(213, 216, 221)` | `none` |
| Control / input (search box) | `.search-input-wide` | dark | `rgb(15, 26, 38)` | `rgb(242, 247, 252)` | `rgb(47, 62, 80)` | `none` |
| Primary button (Send) | `.input-form button[type=submit]` | light | `rgba(0, 0, 0, 0)` | `rgb(255, 255, 255)` | `rgb(255, 255, 255)` | `none` |
| Primary button (Send) | `.input-form button[type=submit]` | dark | `rgba(0, 0, 0, 0)` | `rgb(255, 255, 255)` | `rgb(255, 255, 255)` | `none` |
| Popover (notifications) | `.notification-popover` | light | `rgb(255, 255, 255)` | `rgb(38, 54, 75)` | `rgb(215, 222, 232)` | `rgba(15, 23, 42, 0.2) 0px 14px 34px 0px` |
| Popover (notifications) | `.notification-popover` | dark | `rgb(27, 40, 55)` | `rgb(238, 246, 255)` | `rgb(58, 82, 109)` | `rgba(0, 0, 0, 0.48) 0px 16px 38px 0px` |
| Dialog (confirm delete) | `.modal-content` | light | `rgb(255, 255, 255)` | `rgb(16, 35, 63)` | `rgb(16, 35, 63)` | `rgba(0, 0, 0, 0.15) 0px 8px 30px 0px` |
| Dialog (confirm delete) | `.modal-content` | dark | `rgb(27, 45, 64)` | `rgb(217, 231, 245)` | `rgb(58, 82, 106)` | `rgba(0, 0, 0, 0.52) 0px 22px 55px 0px` |
| Toast (dock notification status) | `.dock-notification-status` | light | `rgba(0, 0, 0, 0)` | `rgb(16, 35, 63)` | `rgba(0, 0, 0, 0)` | `none` |
| Toast (dock notification status) | `.dock-notification-status` | dark | `rgba(0, 0, 0, 0)` | `rgb(242, 247, 252)` | `rgba(0, 0, 0, 0)` | `none` |
| Badge (device status pill) | `.device-status-pill` | light | `rgba(0, 0, 0, 0)` | `rgb(20, 36, 58)` | `rgb(20, 36, 58)` | `none` |
| Badge (device status pill) | `.device-status-pill` | dark | `rgba(0, 0, 0, 0)` | `rgb(243, 247, 251)` | `rgb(243, 247, 251)` | `none` |
| Table-like list (device detail list) | `.device-detail-list` | light | `rgba(0, 0, 0, 0)` | `rgb(20, 36, 58)` | `rgb(20, 36, 58)` | `none` |
| Table-like list (device detail list) | `.device-detail-list` | dark | `rgba(0, 0, 0, 0)` | `rgb(243, 247, 251)` | `rgb(243, 247, 251)` | `none` |
| Node card | `.node-card.favorite` | light | `rgb(255, 247, 223)` | `rgb(16, 35, 63)` | `rgb(226, 189, 69)` | `rgba(47, 115, 217, 0.12) 0px 0px 0px 1px` |
| Node card | `.node-card.favorite` | dark | `rgb(38, 62, 88)` | `rgb(242, 247, 252)` | `rgb(102, 168, 255)` | `rgba(47, 115, 217, 0.12) 0px 0px 0px 1px` |
| Media stage (camera frame) | `.video-frame-wrap` | light | `rgb(232, 234, 237)` | `rgb(16, 35, 63)` | `rgb(16, 35, 63)` | `none` |
| Media stage (camera frame) | `.video-frame-wrap` | dark | `rgb(17, 27, 39)` | `rgb(243, 247, 251)` | `rgb(38, 53, 71)` | `none` |
| Telemetry chart container | `.telemetry-chart-container` | light | `rgb(240, 242, 245)` | `rgb(16, 35, 63)` | `rgb(229, 232, 236)` | `none` |
| Telemetry chart container | `.telemetry-chart-container` | dark | `rgb(26, 37, 53)` | `rgb(242, 247, 252)` | `rgb(38, 60, 82)` | `none` |