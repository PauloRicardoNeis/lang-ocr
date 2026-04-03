# Lang notes

## database
- `sources`: fonte bruta real, como print, imagem ou texto.
- `terms`: entidade canonica da palavra ou expressao.
- `term_forms`: formas vistas da entidade canonica.
- `source_terms`: ligacao entre fonte e termo, com contagem por fonte.
- `term_stats`: resumo agregado para prioridade de estudo.
- fonte bruta continua sendo a verdade; stats sao derivados.

## ocr
- entrada principal: prints de jogos em japones.
- salvar imagem primeiro, depois extrair texto.
- quebrar OCR em blocos curtos, nao em um texto gigante.
- revisar manualmente quando OCR vier confuso.
- guardar contexto: menu, dialogo, item, combate, quest.

## recurrence
- atualizar recorrencia no momento do input.
- evitar recalcular tudo lendo todas as fontes.
- aumentar peso por `count_in_source`.
- contar tambem `distinct_sources`, nao so repeticao bruta.
- manter comando futuro para reconstruir stats do zero.

## flashcard creation
- gerar card a partir de termos prioritarios.
- preferir cards com contexto real da fonte.
- formatos uteis: `JP -> PT`, `leitura -> significado`, `frase com lacuna`.
- separar decks por contexto: menu, combate, dialogo, sistema.
- ignorar ruido cedo para nao poluir revisao.

## desktop capture
- programa local com hotkey para screenshot.
- salvar print e metadata automaticamente.
- enviar print para pipeline de OCR/catalogacao.
- criar rascunho inicial da fonte sem friccao manual.

## priority
- palavras em fontes diferentes valem mais.
- palavras de interface recorrente podem receber penalidade.
- expressoes curtas importantes entram como termo proprio.
- foco inicial em vocabulario util e reaparecente.
