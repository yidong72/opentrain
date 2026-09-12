import httpx
import pytest


@pytest.mark.integration
def test_full_artifact_lifecycle(sdk):
    sdk(
        """
import wandb
from pathlib import Path
settings=wandb.Settings(init_timeout=20,x_disable_stats=True)
with wandb.init(project="artifact-compat",id="producer",settings=settings) as run:
    Path("weights.bin").write_bytes(b"first model")
    artifact=wandb.Artifact("weights",type="model",description="first",metadata={"epoch":1})
    artifact.add_file("weights.bin")
    run.log_artifact(artifact,aliases=["latest","best"]).wait()
    assert artifact.version == "v0"
with wandb.init(project="artifact-compat",id="consumer",settings=settings) as run:
    artifact=run.use_artifact("local/artifact-compat/weights:best")
    path=artifact.download(root="download",skip_cache=True)
    assert Path(path,"weights.bin").read_bytes() == b"first model"
    assert artifact.metadata["epoch"] == 1
    artifact.description="reviewed"
    artifact.metadata["score"] = .95
    artifact.aliases.append("production")
    artifact.save()
api=wandb.Api()
artifact=api.artifact("local/artifact-compat/weights:production")
assert artifact.description == "reviewed"
assert artifact.metadata["score"] == .95
assert len(list(api.run("local/artifact-compat/consumer").used_artifacts())) == 1
assert len(list(api.run("local/artifact-compat/producer").logged_artifacts())) == 1
assert len(list(api.artifact_versions("model","local/artifact-compat/weights"))) == 1
assert len(list(artifact.files())) == 1
artifact.delete(delete_aliases=True)
""",
        timeout=150,
    )


@pytest.mark.integration
def test_shared_sdk_writers(server, sdk):
    sdk(
        '''
import subprocess, sys
from concurrent.futures import ThreadPoolExecutor
code = """
import wandb, sys, time
from pathlib import Path
rank = int(sys.argv[1])
Path(f'rank{rank}').mkdir()
settings=wandb.Settings(mode='shared',x_primary=rank==0,x_label=f'rank{rank}',x_update_finish_state=rank==0,x_disable_stats=True,init_timeout=20)
run=wandb.init(project='shared',id='distributed',dir=f'rank{rank}',settings=settings)
for i in range(12):
    run.log({f'rank{rank}/loss':rank*100+i,'global_step':i})
if rank == 1:
    run.finish()
    Path('worker_done').touch()
else:
    deadline=time.monotonic()+50
    while not Path('worker_done').exists():
        assert time.monotonic()<deadline, 'Secondary writer did not finish'
        time.sleep(.1)
    run.log({'rank0/loss':12,'global_step':12})
    run.finish()
"""
def launch(rank):
    subprocess.run([sys.executable,'-c',code,str(rank)],check=True,timeout=75)
with ThreadPoolExecutor(2) as pool:
    list(pool.map(launch,[0,1]))
''',
        timeout=100,
    )
    runs = httpx.get(server["url"] + "/api/runs", params={"project": "shared"}).json()[
        "runs"
    ]
    assert len(runs) == 1 and runs[0]["state"] == "finished"
    uid = runs[0]["uid"]
    rows = httpx.get(f"{server['url']}/api/runs/{uid}/history").json()["rows"]
    assert len(rows) == 25
    assert len({r["_writer"] for r in rows}) == 2
    assert len({r["_step"] for r in rows}) == 25


@pytest.mark.integration
def test_advanced_tables(server, sdk):
    sdk(
        """
import wandb
import numpy as np
settings=wandb.Settings(init_timeout=20,x_disable_stats=True)
with wandb.init(project="tables-compat",id="tables",settings=settings) as run:
    table=wandb.Table(columns=["step","score"],log_mode="INCREMENTAL")
    for i in range(3):
        table.add_data(i,float(i)/10)
        run.log({"incremental":table})
    mutable=wandb.Table(columns=["id","prediction"],log_mode="MUTABLE")
    mutable.add_data(1,"before");run.log({"mutable":mutable})
    mutable.add_data(2,"after");run.log({"mutable":mutable})
    artifact=wandb.Artifact("advanced",type="dataset")
    left=wandb.Table(columns=["id","label"],data=[[1,"cat"],[2,"dog"]])
    right=wandb.Table(columns=["id","score"],data=[[1,.9],[3,.2]])
    joined=wandb.JoinedTable(left,right,"id")
    artifact.add(joined,"joined")
    artifact.add(wandb.Table(columns=["id","label"],data=[[1,"cat"],[2,"dog"]]),"parts/0")
    artifact.add(wandb.Table(columns=["id","label"],data=[[3,"cat"],[4,"dog"]]),"parts/1")
    partitioned=wandb.data_types.PartitionedTable("parts")
    artifact.add(partitioned,"partitioned")
    artifact.add(wandb.Table(columns=["image"],data=[[wandb.Image(np.zeros((8,8,3),dtype=np.uint8))]]),"images")
    run.log_artifact(artifact).wait()
    run.log({"joined":joined,"partitioned":partitioned})
api=wandb.Api()
artifact=api.artifact("local/tables-compat/advanced:latest")
assert len(artifact.get("parts/0").data)==2
assert len(list(artifact.get("partitioned").iterrows()))==4
""",
        timeout=120,
    )
    runs = httpx.get(
        server["url"] + "/api/runs", params={"project": "tables-compat"}
    ).json()["runs"]
    uid = runs[0]["uid"]
    for key, count in [
        ("incremental", 3),
        ("mutable", 2),
        ("joined", 3),
        ("partitioned", 4),
    ]:
        response = httpx.get(
            f"{server['url']}/api/runs/{uid}/table", params={"key": key}
        )
        assert response.status_code == 200, response.text
        assert response.json()["total"] == count, (key, response.json())
    response = httpx.get(
        f"{server['url']}/api/runs/{uid}/table",
        params={"key": "incremental", "sort": 1, "descending": "true", "limit": 1},
    )
    assert response.json()["data"] == [[2, 0.2]]


@pytest.mark.integration
def test_artifact_references_drafts_and_links(sdk):
    sdk(
        """
import wandb
from pathlib import Path
settings=wandb.Settings(init_timeout=20,x_disable_stats=True)
with wandb.init(project="artifact-links",id="producer",settings=settings) as run:
    Path("data.txt").write_text("original data")
    first=wandb.Artifact("dataset",type="dataset")
    first.add_file("data.txt")
    run.log_artifact(first).wait()
    duplicate=wandb.Artifact("dataset",type="dataset")
    duplicate.add_file("data.txt")
    run.log_artifact(duplicate,aliases=["duplicate"]).wait()
    assert duplicate.id==first.id and duplicate.version=="v0"
    draft=first.new_draft()
    Path("new.txt").write_text("new data")
    draft.add_file("new.txt")
    run.log_artifact(draft).wait()
    assert draft.version=="v1"
    linked=wandb.Artifact("reference",type="dataset")
    linked.add_reference(first.get_entry("data.txt").ref_url(),name="copy.txt")
    run.log_artifact(linked).wait()
    linked.link("local/artifact-links/curated",aliases=["best"])
api=wandb.Api()
artifact=api.artifact("local/artifact-links/reference:latest")
path=artifact.download(root="ref-download",skip_cache=True)
assert Path(path,"copy.txt").read_text()=="original data"
draft=api.artifact("local/artifact-links/dataset:latest")
path=draft.download(root="draft-download",skip_cache=True)
assert Path(path,"data.txt").read_text()=="original data"
assert Path(path,"new.txt").read_text()=="new data"
portfolio=api.artifact("local/artifact-links/curated:best")
assert portfolio.id==artifact.id
portfolio.unlink()
""",
        timeout=150,
    )
