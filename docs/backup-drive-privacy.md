# Church Roster Backup — Privacidade

O Church Roster Backup mantém uma cópia cifrada do Church Roster Bot no Google
Drive do próprio administrador. A aplicação principal continua hospedada na VPS;
esta autorização Google serve exclusivamente para suas cópias de segurança.

## Acesso solicitado ao Google Drive

A permissão `drive.file` permite consultar, criar, alterar e remover somente os
arquivos e pastas usados com este aplicativo. O programa não solicita acesso
geral aos demais arquivos do Drive. A rotina cria uma pasta exclusiva para os
backups e pode consultar a quota de armazenamento disponível.

## Dados e finalidade

O backup contém o banco da aplicação e a configuração necessária à recuperação.
O conteúdo é cifrado na VPS, antes do envio ao Google Drive, com uma senha de
recuperação mantida pelo administrador. O administrador deve guardar essa senha
fora da VPS; sem ela, o conteúdo cifrado não poderá ser recuperado.

O ensaio inicial utiliza exclusivamente voluntários, telefones e cultos
fictícios. Dados reais somente poderão ser incluídos após decisão e configuração
do responsável pela aplicação. O programa não utiliza os dados do Google para
publicidade ou treinamento de modelos de inteligência artificial e não os
transfere a terceiros para esses fins.

## Credenciais e controle

As credenciais de autorização ficam em arquivo protegido na VPS, fora do
repositório público. Elas não são publicadas em logs, documentação ou conversas.
O administrador pode revogar a autorização na página de conexões da sua Conta
Google, interrompendo novos acessos do programa.

Não há compartilhamento público da pasta de backup. Retenção e eventual remoção
de cópias são controladas pelo administrador; o ensaio inicial não faz expurgo
automático de backups. A revogação da autorização não remove cópias já enviadas.
O administrador pode administrar esses arquivos pelo próprio Google Drive.

## Contato e alterações

Dúvidas sobre o programa podem ser registradas no
[repositório do Church Roster Bot](https://github.com/viniciusrodriguesai/church-roster-bot).
Não inclua senhas, tokens, números reais ou dados de membros em issues públicas.
Esta página será atualizada quando o tratamento de dados do backup mudar.

Última atualização: 15 de novembro de 2026.
