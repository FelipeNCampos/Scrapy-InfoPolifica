# Release Windows

O release Windows e gerado pelo GitHub Actions em `.github/workflows/release.yml`.

## Criar uma release

1. Envie as alteracoes para o GitHub.
2. Crie uma tag no formato `vN.N.N`:

```powershell
git tag v1.0.0
git push origin v1.0.0
```

3. Aguarde o workflow `Release MSI`.
4. Baixe o arquivo `ScrapyInfoPolitica-N.N.N-x64.msi` na pagina de Releases do GitHub.

Tambem da para rodar manualmente em `Actions > Release MSI > Run workflow`.

## Observacoes

- O instalador e per-user e instala em `%LOCALAPPDATA%\Programs\ScrapyInfoPolitica`.
- Os dados, perfis de Chrome e outputs padrao ficam em `%LOCALAPPDATA%\ScrapyInfoPolitica`.
- O app precisa do Google Chrome instalado.
- Sem assinatura de codigo, o Windows SmartScreen pode mostrar aviso no primeiro uso.
