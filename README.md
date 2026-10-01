# NK APL VisionEdge — Single Snapshot Inspection

NK APL 單張拍照判定開發版：掃描 SN 後，確認同一張影像中蓋板已關閉且 MCIO
小卡存在。

## 文件入口

1. [目前規格與操作](README_APL_ZH.md)
2. [完整交接手冊](docs/HANDOVER_ZH.md)
3. [文件索引](docs/README.md)
4. [QTI 相機與 MediaMTX 故障排除](docs/QTI_REGISTRY_MEDIAMTX_RECOVERY_ZH.md)
5. [工號與掃碼槍操作](docs/SCANNER_OPERATOR_FLOW_ZH.md)

`docs/archive/` 只保存上游 VisionEdge 與舊流程紀錄，不代表目前規格。

## 啟動

```bash
cd /var/rootdirs/opt/VisionEdge_APL
/usr/bin/python3 -B -u visionedge_server.py
```

頁面：`https://<SmartCam-IP>:8765/edge`

## PC 測試

```powershell
python visionedge_server.py --demo --no-tls --port 8772
python -m unittest discover -s tests -p "test_*.py" -v
```

本機回歸測試包含工號前置、掃碼槍選擇與 SFIS bypass；SmartCam 實機仍須另外驗收。

正式部署不包含 `test_assets/`、`tests/`、`docs/archive/`、Python 快取或 PC
`runtime_data/`。
