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

- [ ] Criar um módulo compartilhado de OCR em `JP/ocr_backends.py`
- [ ] Mover o backend `EasyOCR` para esse módulo
- [ ] Mover a estrutura `OcrBlock` para esse módulo
- [ ] Fazer `single_image_ocr.py` usar o backend compartilhado
- [ ] Adicionar um `EasyOcrAdapter` no worker automático
- [ ] Parar de usar `PaddleOCR` no caminho principal do worker
- [ ] Rodar `EasyOCR` com `detail=1` para receber `bbox + text + confidence`
- [ ] Converter cada detecção em `OcrBlock`
- [ ] Implementar filtro de caixa horizontal
- [ ] Implementar filtro de texto com caracteres japoneses
- [ ] Implementar filtro mínimo de confiança
- [ ] Ordenar as caixas aceitas por leitura (`top`, depois `left`)
- [ ] Montar `raw_text` final a partir das caixas aceitas
- [ ] Rejeitar o job quando não sobrar texto japonês útil
- [ ] Manter o move da imagem para `captures` quando o job for aceito
- [ ] Continuar criando `source` no banco
- [ ] Continuar criando `source_analysis_segments`
- [ ] Continuar criando `source_analysis_candidates`
- [ ] Adicionar logs de quantidade de caixas detectadas e aceitas
- [ ] Adicionar logs de rejeição por `vertical`, `non-japanese` e `low-confidence`
- [ ] Validar o worker em uma imagem do spool
- [ ] Validar em pelo menos 5 screenshots reais
- [ ] Confirmar que o PC não trava durante `process next OCR job`
- [ ] Atualizar `README.md` com o novo fluxo automático
- [ ] Atualizar `requirements-capture.txt` para refletir o caminho recomendado

## Acceptance

- [ ] O worker processa 1 screenshot sem travar a máquina
- [ ] Screenshots com texto horizontal geram `raw_text` útil
- [ ] Screenshots sem japonês são rejeitadas
- [ ] O banco recebe drafts corretamente
- [ ] O tray continua funcionando com `Process next OCR job`
