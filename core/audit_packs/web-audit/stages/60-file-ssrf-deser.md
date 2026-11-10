# Stage 60: File Upload, SSRF & Deserialization (文件与内部网络探测)
<!-- L1:START -->
1. 文件接口测试：测试后缀绕过、目录穿越 (Zip Slip)、MIME 欺骗、内容多态。
2. SSRF 探测：测试 webhook/图片抓取接口对内网 IP (127.0.0.1, 169.254.169.254) 访问。
3. 反序列化探测：检测敏感序列化流 (Java/Python/PHP) 与已知 gadget 链。
4. 产出可达性与读取证据并记录受影响端点。
<!-- L1:END -->
<!-- L2:START -->
## 操作指南
- SSRF 探测时严格禁止对生产内网进行横向端口扫描，仅验证回显与云元数据可达性。
- 文件上传优先上传安全探针 (如仅含 phpinfo 或静态文本的代码)，禁止上传常驻 webshell。
<!-- L2:END -->
