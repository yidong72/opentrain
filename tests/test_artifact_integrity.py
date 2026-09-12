import base64
import hashlib

from fastapi.testclient import TestClient

from open_train.app import create_app


def test_multipart_commit_checksums_and_immutability(tmp_path):
    client = TestClient(create_app(tmp_path))

    def gql(query, variables=None):
        response = client.post(
            "/graphql", json={"query": query, "variables": variables}
        ).json()
        assert not response.get("errors"), response
        return response["data"]

    uid = gql(
        'mutation{upsertBucket(input:{name:"producer",modelName:"p"}){bucket{id}}}'
    )["upsertBucket"]["bucket"]["id"]
    artifact = gql(
        'mutation{createArtifact(input:{entityName:"local",projectName:"p",runName:"producer",artifactCollectionName:"large",artifactTypeName:"model",digest:"test"}){artifact{id}}}'
    )["createArtifact"]["artifact"]["id"]
    contents = [b"first part", b"second part"]
    checksum = base64.b64encode(hashlib.md5(b"".join(contents)).digest()).decode()
    parts = [
        {"partNumber": i + 1, "hexMD5": hashlib.md5(c).hexdigest()}
        for i, c in enumerate(contents)
    ]
    query = "mutation($i:CreateArtifactFilesInput!){createArtifactFiles(input:$i){files{edges{node{storagePath uploadMultipartUrls{uploadID uploadUrlParts{partNumber uploadUrl}}}}}}}"
    file = gql(
        query,
        {
            "i": {
                "artifactFiles": [
                    {
                        "artifactID": artifact,
                        "name": "model.bin",
                        "md5": checksum,
                        "uploadPartsInput": parts,
                    }
                ]
            }
        },
    )["createArtifactFiles"]["files"]["edges"][0]["node"]
    upload = file["uploadMultipartUrls"]
    for part in reversed(upload["uploadUrlParts"]):
        data = contents[part["partNumber"] - 1]
        response = client.put(part["uploadUrl"], content=data)
        assert response.status_code == 200
        assert response.headers["etag"].strip('"') == hashlib.md5(data).hexdigest()
        assert client.put(part["uploadUrl"], content=data).status_code == 200
    complete = "mutation($i:CompleteMultipartUploadArtifactInput!){completeMultipartUploadArtifact(input:$i){digest}}"
    variables = {
        "i": {
            "artifactID": artifact,
            "storagePath": file["storagePath"],
            "uploadID": upload["uploadID"],
            "completedParts": parts,
            "completeMultipartAction": "Complete",
        }
    }
    assert (
        gql(complete, variables)["completeMultipartUploadArtifact"]["digest"]
        == checksum
    )
    assert (
        gql(complete, variables)["completeMultipartUploadArtifact"]["digest"]
        == checksum
    )
    path = f"/files/{uid}/artifacts/{artifact}/model.bin"
    assert client.get(path).content == b"".join(contents)
    assert client.put(path, content=b"corrupt").status_code == 409
    manifest = gql(
        "mutation($i:CreateArtifactManifestInput!){createArtifactManifest(input:$i){artifactManifest{file{uploadUrl}}}}",
        {
            "i": {
                "artifactID": artifact,
                "name": "wandb_manifest.json",
                "digest": "manifest",
                "entityName": "local",
                "projectName": "p",
                "runName": "producer",
                "type": "FULL",
            }
        },
    )["createArtifactManifest"]["artifactManifest"]["file"]["uploadUrl"]
    assert (
        client.put(
            manifest,
            json={
                "version": 1,
                "contents": {
                    "model.bin": {"digest": checksum, "size": len(b"".join(contents))}
                },
            },
        ).status_code
        == 200
    )
    gql(
        "mutation($id:ID!){commitArtifact(input:{artifactID:$id}){artifact{state}}}",
        {"id": artifact},
    )
    assert client.put(path, content=b"".join(contents)).status_code == 200
    assert client.put(manifest, json={"contents": {}}).status_code == 409


def test_shared_retries_are_idempotent_and_conflicts_rejected(tmp_path):
    client = TestClient(create_app(tmp_path))
    store = client.app.state.store
    uid = store.upsert({"name": "r", "modelName": "p"})[0]["uid"]
    path = "/files/local/p/r/file_stream"
    headers = {
        "X-WANDB-USE-ASYNC-FILESTREAM": "true",
        "X-WANDB-ASYNC-CLIENT-ID": "worker-a",
    }
    payload = {
        "files": {
            "wandb-history.jsonl": {"offset": 0, "content": ['{"loss":1,"_step":0}']}
        }
    }
    assert client.post(path, headers=headers, json=payload).status_code == 200
    assert client.post(path, headers=headers, json=payload).status_code == 200
    payload["files"]["wandb-history.jsonl"]["content"] = ['{"loss":2,"_step":0}']
    assert client.post(path, headers=headers, json=payload).status_code == 409
    headers["X-WANDB-ASYNC-CLIENT-ID"] = "worker-b"
    assert client.post(path, headers=headers, json=payload).status_code == 200
    assert len(store.lines(uid, "wandb-history.jsonl")) == 2
    client.post(path, headers=headers, json={"complete": True, "exitcode": 0})
    payload["files"]["wandb-history.jsonl"]["offset"] = 1
    assert client.post(path, headers=headers, json=payload).status_code == 409


def test_table_reference_security_and_limits(tmp_path):
    client = TestClient(create_app(tmp_path))
    uid = client.app.state.store.upsert({"name": "table"})[0]["uid"]
    client.put(
        f"/files/{uid}/unsafe.table.json",
        json={
            "_type": "table-file",
            "artifact_path": "http://169.254.169.254/latest/meta-data",
        },
    )
    response = client.get(
        f"/api/runs/{uid}/table", params={"path": "unsafe.table.json"}
    )
    assert response.status_code == 422
    assert "External references" in response.text
    client.put(
        f"/files/{uid}/cycle.table.json",
        json={"_type": "table-file", "path": "cycle.table.json"},
    )
    assert (
        client.get(
            f"/api/runs/{uid}/table", params={"path": "cycle.table.json"}
        ).status_code
        == 422
    )
