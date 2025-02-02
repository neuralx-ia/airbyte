#
# Copyright (c) 2023 Airbyte, Inc., all rights reserved.
#
import json
import uuid
from typing import List, Tuple

import anyio
from ci_connector_ops.pipelines import builds
from ci_connector_ops.pipelines.actions import environments, run_steps
from ci_connector_ops.pipelines.actions.remote_storage import upload_to_gcs
from ci_connector_ops.pipelines.bases import ConnectorReport, Step, StepResult, StepStatus
from ci_connector_ops.pipelines.contexts import PublishConnectorContext
from ci_connector_ops.pipelines.pipelines import metadata
from ci_connector_ops.pipelines.utils import with_stderr, with_stdout
from dagger import Container, File, QueryError


class CheckConnectorImageDoesNotExist(Step):
    title = "Check if the connector docker image does not exist on the registry."

    async def _run(self) -> StepResult:
        manifest_inspect = (
            environments.with_docker_cli(self.context)
            .with_env_variable("CACHEBUSTER", str(uuid.uuid4()))
            .with_exec(["sh", "-c", f"docker manifest inspect {self.context.docker_image_name} || true"])
        )
        manifest_inspect_stderr = await with_stderr(manifest_inspect)
        manifest_inspect_stdout = await with_stdout(manifest_inspect)
        if "no such manifest" in manifest_inspect_stderr:
            return StepResult(self, status=StepStatus.SUCCESS, stdout=f"No manifest found for {self.context.docker_image_from_metadata}.")
        else:
            try:
                manifests = json.loads(manifest_inspect_stdout.replace("\n", "")).get("manifests", [])
                available_platforms = {f"{manifest['platform']['os']}/{manifest['platform']['architecture']}" for manifest in manifests}
                if set(builds.BUILD_PLATFORMS).issubset(available_platforms):
                    return StepResult(self, status=StepStatus.SKIPPED, stderr=f"{self.context.docker_image_from_metadata} already exists.")
                else:
                    return StepResult(
                        self, status=StepStatus.SUCCESS, stdout=f"No all manifests found for {self.context.docker_image_from_metadata}."
                    )
            except json.JSONDecodeError:
                return StepResult(self, status=StepStatus.FAILURE, stderr=manifest_inspect_stderr, stdout=manifest_inspect_stdout)


class BuildConnectorForPublish(Step):
    title = "Build connector for publish"

    async def _run(self) -> StepResult:
        build_connectors_results = (await builds.run_connector_build(self.context)).values()
        for build_result in build_connectors_results:
            if build_result.status is not StepStatus.SUCCESS:
                return build_result
        built_connectors_platform_variants = [step_result.output_artifact for step_result in build_connectors_results]

        return StepResult(self, status=StepStatus.SUCCESS, output_artifact=built_connectors_platform_variants)


class PushConnectorImageToRegistry(Step):
    title = "Push connector image to registry"

    @property
    def latest_docker_image_name(self):
        return f"{self.context.metadata['dockerRepository']}:latest"

    async def _run(self, built_containers_per_platform: List[Container]) -> StepResult:
        try:
            image_ref = await built_containers_per_platform[0].publish(
                f"docker.io/{self.context.docker_image_name}", platform_variants=built_containers_per_platform[1:]
            )
            if not self.context.pre_release:
                image_ref = await built_containers_per_platform[0].publish(
                    f"docker.io/{self.latest_docker_image_name}", platform_variants=built_containers_per_platform[1:]
                )
            return StepResult(self, status=StepStatus.SUCCESS, stdout=f"Published {image_ref}")
        except QueryError as e:
            return StepResult(self, status=StepStatus.FAILURE, stderr=str(e))


class InvalidSpecOutputError(Exception):
    pass


class UploadSpecToCache(Step):
    title = "Upload connector spec to spec cache bucket"
    default_spec_file_name = "spec.json"
    cloud_spec_file_name = "spec.cloud.json"

    @property
    def spec_key_prefix(self):
        return "specs/" + self.context.docker_image_name.replace(":", "/")

    @property
    def cloud_spec_key(self):
        return f"{self.spec_key_prefix}/{self.cloud_spec_file_name}"

    @property
    def oss_spec_key(self):
        return f"{self.spec_key_prefix}/{self.default_spec_file_name}"

    def _parse_spec_output(self, spec_output: str) -> str:
        for line in spec_output.split("\n"):
            try:
                parsed_json = json.loads(line)
                if parsed_json["type"] == "SPEC":
                    return json.dumps(parsed_json)
            except (json.JSONDecodeError, KeyError):
                continue
        raise InvalidSpecOutputError("Could not parse the output of the spec command.")

    async def _get_connector_spec(self, connector: Container, deployment_mode: str) -> str:
        spec_output = await connector.with_env_variable("DEPLOYMENT_MODE", deployment_mode).with_exec(["spec"]).stdout()
        return self._parse_spec_output(spec_output)

    def _get_spec_as_file(self, spec: str, name="spec_to_cache.json") -> File:
        return self.context.get_connector_dir().with_new_file(name, spec).file(name)

    async def _run(self, built_connector: Container) -> StepResult:
        oss_spec: str = await self._get_connector_spec(built_connector, "OSS")
        cloud_spec: str = await self._get_connector_spec(built_connector, "CLOUD")

        specs_to_uploads: List[Tuple[str, File]] = [(self.oss_spec_key, self._get_spec_as_file(oss_spec))]

        if oss_spec != cloud_spec:
            specs_to_uploads.append(self.cloud_spec_key, self._get_spec_as_file(cloud_spec, "cloud_spec_to_cache.json"))

        for key, file in specs_to_uploads:
            exit_code, stdout, stderr = await upload_to_gcs(
                self.context.dagger_client,
                file,
                key,
                self.context.spec_cache_bucket_name,
                self.context.spec_cache_gcs_credentials_secret,
            )
            if exit_code != 0:
                return StepResult(self, status=StepStatus.FAILURE, stdout=stdout, stderr=stderr)
        return StepResult(self, status=StepStatus.SUCCESS)


async def run_connector_publish_pipeline(context: PublishConnectorContext, semaphore: anyio.Semaphore) -> ConnectorReport:
    """Run a publish pipeline for a single connector.

    1. Validate the metadata file.
    2. Check if the connector image already exists.
    3. Build the connector, with platform variants.
    4. Upload its spec to the spec cache bucket.
    5. Push the connector to DockerHub, with platform variants.
    6. Upload its metadata file to the metadata service bucket.

    Returns:
        ConnectorReport: The reports holding publish results.
    """
    async with semaphore:
        async with context:

            metadata_upload_step = metadata.MetadataUpload(context)
            steps_before_ready_to_publish = [
                metadata.MetadataValidation(context, context.metadata_path),
                CheckConnectorImageDoesNotExist(context),
            ]
            steps_before_ready_to_publish_results = await run_steps(steps_before_ready_to_publish)

            if steps_before_ready_to_publish_results[-1].status is not StepStatus.SUCCESS:
                if steps_before_ready_to_publish_results[-1].status is StepStatus.SKIPPED:
                    context.logger.info(
                        "The connector version is already published. Let's upload metadata.yaml to GCS even if no version bump happened."
                    )
                    metadata_upload_results = await metadata_upload_step.run()
                    context.report = ConnectorReport(
                        context, steps_before_ready_to_publish_results + [metadata_upload_results], name="PUBLISH RESULTS"
                    )
                else:
                    context.report = ConnectorReport(context, steps_before_ready_to_publish_results, name="PUBLISH RESULTS")
                return context.report
            else:
                step_results = steps_before_ready_to_publish_results

            build_connector_results = await BuildConnectorForPublish(context).run()
            step_results.append(build_connector_results)
            if build_connector_results.status is not StepStatus.SUCCESS:
                context.report = ConnectorReport(context, step_results, name="PUBLISH RESULTS")
                return context.report

            built_connector_platform_variants = build_connector_results.output_artifact

            steps_to_publish = [
                (
                    UploadSpecToCache(context),
                    (built_connector_platform_variants[0],),
                ),
                (PushConnectorImageToRegistry(context), (built_connector_platform_variants,)),
                metadata_upload_step,
            ]

            step_results = await run_steps(steps_to_publish, results=step_results)
            context.report = ConnectorReport(context, step_results, name="PUBLISH RESULTS")
        return context.report
