# JP Surge Radar

日本株 3000 円以下の全銘柄から、10 営業日以内に +20% に届く銘柄を Claude が毎日選び、
その成否を教師データとして選び方を改善していく仕組み。

- 設計と原則: [docs/DESIGN.md](docs/DESIGN.md)
- ラベル定義: [docs/LABELS.md](docs/LABELS.md)
- 候補選定の手順: [procedures/select.md](procedures/select.md)
- 材料ラベル付けの手順: [procedures/label.md](procedures/label.md)

2026-09-26 に旧版(ルールと機械学習のスコアリング)を破棄して作り直した。旧版は git 履歴にある。
