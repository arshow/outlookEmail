# 手动获取邮件进度

单邮箱和聚合收件箱点击获取按钮后，显示批次、耗时、已读取和已展示的邮件数。
单邮箱按文件夹统计，聚合收件箱按邮箱统计；进度条表示当前批次已结束的请求比例，
成功、失败、部分失败分别计数，超时是其中的补充统计。切换邮箱或文件夹会停止旧页面的进度轮询。

邮件列表 GET 请求可附加 `progress_id`（32 位小写十六进制随机标识，每批使用新值）。
服务端仍按原接口返回邮件结果，并额外返回 `fetch_progress`。

网页的手动获取会同时使用 `async=1`：接口立即返回 HTTP 202 和 `pending=true`，
收信在后台执行。前端通过进度接口附加 `result=1`，用短请求轮询进度和最终 `result`，
避免等待 Graph/IMAP 的长 HTTP 连接被反向代理提前以 504 中断。原同步接口保持兼容。
后台任务完成结果保留 15 分钟，最多保留 32 个任务、同时执行 8 个任务；服务重启后清空。

`GET /api/emails/fetch-progress/<progress_id>` 需要正常 Web 登录，返回：

```json
{"success":true,"progress":{"state":"running","total":2,"completed":1,"successful":1,"failed":0,"partial":0,"timed_out":0,"fetched":10,"active":["junkemail"],"elapsed_seconds":5.2}}
```

请求尚未开始、标识不存在或已过期时，`progress` 为 `null`。状态仅保存在当前单 worker 内存中，
30 分钟无更新后过期，服务重启后清空；最多保留 128 个请求。该接口不返回邮件正文或凭据。
