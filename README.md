# Scrapy InfoPolitica

Projeto academico para coleta automatizada de publicacoes em redes sociais relacionadas a palavras-chave informadas pelo usuario.

O sistema realiza buscas no Google usando filtros por site, encontra resultados do Facebook e Instagram, acessa os links encontrados e organiza os dados extraidos em arquivos JSON e Excel.

## Objetivo

O objetivo do projeto e apoiar pesquisas sobre circulacao de conteudos em redes sociais, permitindo coletar postagens, videos, reels, stories e perfis relacionados a um termo de busca.

Exemplo de uso:

```text
"eleicoes municipais" em resultados do Facebook e Instagram
```

A partir da palavra ou expressao informada, o sistema busca paginas publicas relacionadas e salva os resultados para analise posterior.

## Funcionalidades

- Busca por palavras-chave no Google.
- Filtro por fonte: Facebook, Instagram ou ambas.
- Coleta de resultados encontrados nas paginas do Google.
- Extracao de dados de publicacoes quando disponiveis.
- Separacao dos resultados por tipo em planilhas.
- Exportacao em JSON e XLSX.
- Interface grafica quando o programa e aberto sem parametros.
- Execucao por terminal para uso mais tecnico.
- Suporte a login salvo em perfil local do Chrome.
- Controle de quantidade de workers para acelerar a coleta do Facebook.
- Review de outputs ja gerados para tentar reprocessar dados pendentes do Instagram.
- Geracao automatica de instalador Windows `.msi` via GitHub Actions.

## Dados Extraidos

No Facebook, o projeto separa resultados em abas como:

- Perfil
- Videos
- Reels
- Posts
- Stories
- Fotos
- Permalinks

Para publicacoes, quando a pagina permite acesso, sao extraidos campos como:

- data
- perfil
- verificacao do perfil
- numero de comentarios
- descricao
- numero de visualizacoes
- numero de reacoes
- status e erro de coleta

No Instagram, os resultados sao separados por:

- perfil
- post
- reel
- nao identificado

Para posts e reels, quando possivel, sao extraidos:

- perfil publicador
- verificacao
- descricao
- numero de likes
- numero de comentarios
- numero de reposts
- status e erro de coleta

## Requisitos

Para rodar pelo codigo-fonte:

- Python 3.12 ou superior
- Google Chrome instalado
- Conexao com a internet
- Windows recomendado, principalmente por causa das notificacoes e do instalador `.msi`

## Instalacao Local

Clone o repositorio:

```powershell
git clone <url-do-repositorio>
cd Scrapy-InfoPolifica
```

Crie e ative o ambiente virtual:

```powershell
python -m venv venv
.\venv\Scripts\activate
```

Instale as dependencias:

```powershell
pip install -r requirements.txt
```

## Uso Pela Interface Grafica

Execute o programa sem parametros:

```powershell
python src/main.py
```

A janela permite configurar:

- string de busca
- fontes: Facebook e/ou Instagram
- datas `after` e `before`
- quantidade de workers
- pasta de output
- pasta de output para review
- abertura de login para as redes marcadas

## Uso Pelo Terminal

Buscar apenas no Facebook:

```powershell
python src/main.py "string de busca" --face
```

Buscar no Facebook e Instagram:

```powershell
python src/main.py "string de busca" --face --insta
```

Buscar com datas:

```powershell
python src/main.py "string de busca" --face --after 2024-01-01 --before 2024-12-31
```

Usar mais workers no Facebook:

```powershell
python src/main.py "string de busca" --face --workers 4
```

Abrir login das redes:

```powershell
python src/main.py --login --face --insta
```

Reprocessar output do Instagram:

```powershell
python src/main.py --review output/01-05-26--10-30
```

## Saida

Os arquivos sao salvos em `output/<data-hora>/`.

Cada fonte gera sua propria pasta:

```text
output/
  01-05-26--10-30/
    face/
      facebook.json
      facebook.xlsx
    insta/
      insta.json
      insta.xlsx
```

Quando o programa e instalado via `.msi`, os arquivos padrao ficam em:

```text
%LOCALAPPDATA%\ScrapyInfoPolitica\output
```

## Status e Erros

Cada registro possui campos de status e erro.

Exemplos de erro:

- `dados_publicacao_nao_encontrados`: a pagina foi aberta, mas os dados esperados nao foram encontrados.
- `login_obrigatorio`: a rede social redirecionou para uma pagina de login.
- erros de captcha ou bloqueio temporario podem exigir resolucao manual no navegador.

## Release Windows

O projeto possui workflow para gerar instalador `.msi` automaticamente no GitHub Actions.

Para criar uma nova release:

```powershell
git add .
git commit -m "Descricao da mudanca"
git push

git tag v1.0.0
git push origin v1.0.0
```

O GitHub Actions ira gerar:

- `ScrapyInfoPolitica.exe`
- `ScrapyInfoPolitica-<versao>-x64.msi`

Os arquivos ficam disponiveis em **GitHub > Releases**.

Mais detalhes estao em `BUILD_RELEASE.md`.

## Observacoes Importantes

- O projeto utiliza automacao de navegador com Selenium.
- As redes sociais podem alterar a estrutura das paginas a qualquer momento.
- Alguns resultados podem exigir login.
- Captchas e bloqueios temporarios podem ocorrer durante buscas repetidas.
- O uso deve respeitar os termos das plataformas e a finalidade academica do projeto.
- O projeto foi desenvolvido para fins educacionais e de pesquisa.

## Estrutura do Projeto

```text
src/
  main.py
  face/
    runner.py
  insta/
    runner.py
  notifier.py

.github/workflows/
  release.yml

packaging/
  ScrapyInfoPolitica.wxs
```

## Autores

Projeto desenvolvido como trabalho academico para coleta e organizacao de informacoes publicas relacionadas a termos de busca em redes sociais.
