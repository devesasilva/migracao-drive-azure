import io
import json
import os
from typing import Optional

import streamlit as st
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from azure.storage.blob import BlobServiceClient, ContainerClient
from azure.core.exceptions import ResourceExistsError

st.set_page_config(
    page_title="Migração Google Drive → Azure Blob",
    page_icon="☁️",
    layout="wide",
)

st.title("Migração de arquivos")
st.caption("Copie arquivos de uma pasta do Google Drive para um contêiner do Azure Blob Storage.")

GOOGLE_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
EXPORT_TYPES = {
    "application/vnd.google-apps.document": (
        "application/pdf",
        ".pdf",
    ),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
    "application/vnd.google-apps.presentation": (
        "application/pdf",
        ".pdf",
    ),
}

def make_drive_service(service_account_info: dict):
    credentials = Credentials.from_service_account_info(
        service_account_info,
        scopes=GOOGLE_SCOPES,
    )
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def make_container_client(sas_url: str, connection_string: str, container_name: str):
    if sas_url.strip():
        # SAS URL deve ser a URL completa do contêiner.
        return ContainerClient.from_container_url(sas_url.strip())

    if connection_string.strip():
        service = BlobServiceClient.from_connection_string(
            connection_string.strip()
        )

        container = service.get_container_client(
            container_name.strip().lower()
        )

        try:
            container.create_container()
            print(f"Contêiner criado: {container_name}")
        except ResourceExistsError:
            print(f"Contêiner já existente: {container_name}")

        return container

    raise ValueError("Informe a Blob SAS URL ou a Connection String.")

def list_drive_files(service, folder_id: str):
    files = []
    page_token = None
    while True:
        response = service.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            fields="nextPageToken, files(id, name, mimeType, size, modifiedTime)",
            pageSize=1000,
            pageToken=page_token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        files.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return files


def download_drive_file(service, file_metadata: dict):
    file_id = file_metadata["id"]
    mime_type = file_metadata.get("mimeType", "")

    if mime_type == "application/vnd.google-apps.folder":
        raise ValueError("Pastas não são transferidas; somente arquivos.")

    if mime_type in EXPORT_TYPES:
        export_mime, extension = EXPORT_TYPES[mime_type]
        request = service.files().export_media(fileId=file_id, mimeType=export_mime)
        output_name = file_metadata["name"] + extension
    else:
        request = service.files().get_media(fileId=file_id)
        output_name = file_metadata["name"]

    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    buffer.seek(0)
    return output_name, buffer


def list_blob_files(container_client):
    return list(container_client.list_blobs())


def migrate_files(service, container_client, files, status_area):
    results = []
    transferable = [f for f in files if f.get("mimeType") != "application/vnd.google-apps.folder"]

    if not transferable:
        status_area.info("Não há arquivos para transferir nesta pasta.")
        return results

    progress = st.progress(0, text="Iniciando transferência...")
    for index, metadata in enumerate(transferable, start=1):
        name = metadata.get("name", "arquivo sem nome")
        try:
            output_name, content = download_drive_file(service, metadata)
            container_client.upload_blob(
                name=output_name,
                data=content,
                overwrite=True,
            )
            results.append((name, output_name, "Sucesso", "Arquivo enviado ao Azure."))
            status_area.success(f"✓ {name} → {output_name}")
        except Exception as exc:
            results.append((name, "", "Erro", str(exc)))
            status_area.error(f"✗ {name}: {exc}")
        progress.progress(index / len(transferable), text=f"Processando {index} de {len(transferable)}")
    return results


with st.sidebar:
    st.header("Configurações")
    st.subheader("Google Drive")
    uploaded_key = st.file_uploader(
        "Arquivo JSON da Service Account",
        type=["json"],
        help="A chave é utilizada apenas durante esta sessão e não deve ser enviada ao GitHub.",
    )
    folder_id = st.text_input(
        "ID da pasta do Google Drive",
        placeholder="Cole aqui o ID da pasta",
    )
    st.divider()
    st.subheader("Azure Blob Storage")
    container_name = st.text_input(
        "Nome do contêiner",
        value="aluno-laine",
        help="Substitua xxx pelo seu nome, conforme o enunciado.",
    )
    auth_method = st.radio(
        "Método de autenticação",
        ["Blob SAS URL", "Connection String"],
    )
    sas_url = ""
    connection_string = ""
    if auth_method == "Blob SAS URL":
        sas_url = st.text_input(
            "Blob SAS URL",
            value="",
            placeholder="Cole aqui a URL SAS do contêiner",
            type="password",
        )
    else:
        connection_string = st.text_input(
            "Connection String",
            value="",
            placeholder="Cole aqui a Connection String",
            type="password",
        )

st.info(
    "Preencha as configurações na barra lateral. Para acessar a pasta, "
    "compartilhe-a com o e-mail da Service Account com permissão de leitor."
)

col1, col2 = st.columns(2)
drive_service = None
container_client = None
config_errors = []

if uploaded_key is None:
    config_errors.append("Envie o JSON da Service Account do Google.")
else:
    try:
        service_account_info = json.loads(uploaded_key.getvalue().decode("utf-8"))
        drive_service = make_drive_service(service_account_info)
    except Exception as exc:
        config_errors.append(f"Não foi possível autenticar no Google Drive: {exc}")

if not container_name.strip():
    config_errors.append("Informe o nome do contêiner.")
else:
    try:
        container_client = make_container_client(sas_url, connection_string, container_name)
    except Exception as exc:
        config_errors.append(f"Não foi possível configurar o Azure: {exc}")

with col1:
    st.subheader("Origem — Google Drive")
    if st.button("Listar arquivos do Google Drive", use_container_width=True):
        if drive_service and folder_id.strip():
            try:
                st.session_state["drive_files"] = list_drive_files(drive_service, folder_id.strip())
                st.success(f"{len(st.session_state['drive_files'])} item(ns) encontrado(s).")
            except Exception as exc:
                st.error(f"Erro ao listar o Google Drive: {exc}")
        else:
            st.warning("Envie a Service Account e informe o ID da pasta.")

    drive_files = st.session_state.get("drive_files", [])
    if drive_files:
        st.dataframe(
            [
                {
                    "Nome": item.get("name"),
                    "Tipo": "Pasta" if item.get("mimeType") == "application/vnd.google-apps.folder" else "Arquivo",
                    "Tamanho (bytes)": item.get("size", "—"),
                    "Modificado em": item.get("modifiedTime", "—"),
                }
                for item in drive_files
            ],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.caption("A listagem da origem aparecerá aqui.")

with col2:
    st.subheader("Destino — Azure Blob Storage")
    if st.button("Listar arquivos do Blob Storage", use_container_width=True):
        if container_client:
            try:
                st.session_state["blob_files"] = list_blob_files(container_client)
                st.success(f"{len(st.session_state['blob_files'])} blob(s) encontrado(s).")
            except Exception as exc:
                st.error(f"Erro ao listar o Azure Blob Storage: {exc}")
        else:
            st.warning("Configure a autenticação do Azure.")

    blob_files = st.session_state.get("blob_files", [])
    if blob_files:
        st.dataframe(
            [
                {
                    "Nome": blob.name,
                    "Tamanho (bytes)": blob.size,
                    "Última modificação": str(blob.last_modified),
                }
                for blob in blob_files
            ],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.caption("A listagem do destino aparecerá aqui.")

st.divider()
st.subheader("Transferência")
if config_errors:
    with st.expander("Verificar configurações", expanded=True):
        for error in config_errors:
            st.warning(error)

can_migrate = bool(drive_service and container_client and folder_id.strip())
if st.button("Iniciar migração", type="primary", use_container_width=True, disabled=not can_migrate):
    try:
        source_files = list_drive_files(drive_service, folder_id.strip())
        st.write(f"Arquivos encontrados na origem: **{len(source_files)}**")
        status_area = st.container()
        migration_results = migrate_files(drive_service, container_client, source_files, status_area)
        successful = sum(1 for result in migration_results if result[2] == "Sucesso")
        failed = sum(1 for result in migration_results if result[2] == "Erro")
        st.session_state["migration_results"] = migration_results
        st.divider()
        st.subheader("Resumo da migração")
        metric1, metric2, metric3 = st.columns(3)
        metric1.metric("Transferidos", successful)
        metric2.metric("Com erro", failed)
        metric3.metric("Total processado", len(migration_results))
        if migration_results:
            st.dataframe(
                [
                    {"Arquivo de origem": r[0], "Nome no destino": r[1] or "—", "Status": r[2], "Detalhe": r[3]}
                    for r in migration_results
                ],
                use_container_width=True,
                hide_index=True,
            )
    except Exception as exc:
        st.error(f"Falha ao executar a migração: {exc}")

st.caption(
    "Observação: esta aplicação copia os arquivos; ela não exclui os arquivos originais do Google Drive."
)
