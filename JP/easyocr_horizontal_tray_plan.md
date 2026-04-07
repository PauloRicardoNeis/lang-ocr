# EasyOCR Horizontal Tray Plan

## Goal

Trocar o OCR automático do tray para um fluxo focado em texto horizontal normal, usando `EasyOCR`.

## Scope

- Captura do tray continua igual.
- OCR automático sai de `PaddleOCR` e vai para `EasyOCR`.
- O worker aceita só caixas horizontais com japonês.
- O resultado continua sendo salvo como draft no banco.

## Out of Scope

- `manga-ocr` no fluxo automático
- ROI manual por screenshot
- templates por site/jogo
- correção linguística avançada
- promoção automática para termos oficiais

## TODO

- [x] Criar um módulo compartilhado de OCR em `JP/ocr_backends.py`
- [x] Mover o backend `EasyOCR` para esse módulo
- [x] Mover a estrutura `OcrBlock` para esse módulo
- [x] Fazer `single_image_ocr.py` usar o backend compartilhado
- [x] Adicionar um `EasyOcrAdapter` no worker automático
- [x] Parar de usar `PaddleOCR` no caminho principal do worker
- [x] Rodar `EasyOCR` com `detail=1` para receber `bbox + text + confidence`
- [x] Converter cada detecção em `OcrBlock`
- [x] Implementar filtro de caixa horizontal
- [x] Implementar filtro de texto com caracteres japoneses
- [x] Implementar filtro mínimo de confiança
- [x] Ordenar as caixas aceitas por leitura (`top`, depois `left`)
- [x] Montar `raw_text` final a partir das caixas aceitas
- [x] Rejeitar o job quando não sobrar texto japonês útil
- [x] Manter o move da imagem para `captures` quando o job for aceito
- [x] Continuar criando `source` no banco
- [x] Continuar criando `source_analysis_segments`
- [x] Continuar criando `source_analysis_candidates`
- [x] Adicionar logs de quantidade de caixas detectadas e aceitas
- [x] Adicionar logs de rejeição por `vertical`, `non-japanese` e `low-confidence`
- [x] Validar o worker em uma imagem do spool
- [ ] Validar em pelo menos 5 screenshots reais
- [ ] Confirmar que o PC não trava durante `process next OCR job`
- [ ] Atualizar `README.md` com o novo fluxo automático
- [ ] Atualizar `requirements-capture.txt` para refletir o caminho recomendado

## Acceptance

- [x] O worker processa 1 screenshot sem travar a máquina
- [ ] Screenshots com texto horizontal geram `raw_text` útil
- [ ] Screenshots sem japonês são rejeitadas
- [x] O banco recebe drafts corretamente
- [ ] O tray continua funcionando com `Process next OCR job`
