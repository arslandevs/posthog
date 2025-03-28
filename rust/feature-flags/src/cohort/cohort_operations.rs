use std::collections::HashSet;
use std::sync::Arc;
use tracing::instrument;

use crate::cohort::cohort_models::{Cohort, CohortId, CohortProperty, InnerCohortProperty};
use crate::{
    api::errors::FlagError, client::database::Client as DatabaseClient,
    properties::property_models::PropertyFilter,
};

impl Cohort {
    /// Returns all cohorts for a given team
    #[instrument(skip_all)]
    pub async fn list_from_pg(
        client: Arc<dyn DatabaseClient + Send + Sync>,
        project_id: i64,
    ) -> Result<Vec<Cohort>, FlagError> {
        let mut conn = client.get_connection().await.map_err(|e| {
            tracing::error!("Failed to get database connection: {}", e);
            FlagError::DatabaseUnavailable
        })?;

        let query = r#"
            SELECT c.id,
                  c.name,
                  c.description,
                  c.team_id,
                  c.deleted,
                  c.filters,
                  c.query,
                  c.version,
                  c.pending_version,
                  c.count,
                  c.is_calculating,
                  c.is_static,
                  c.errors_calculating,
                  c.groups,
                  c.created_by_id
              FROM posthog_cohort AS c
              JOIN posthog_team AS t ON (c.team_id = t.id)
            WHERE t.project_id = $1
            AND c.deleted = false
        "#;
        let cohorts = sqlx::query_as::<_, Cohort>(query)
            .bind(project_id)
            .fetch_all(&mut *conn)
            .await
            .map_err(|e| {
                tracing::error!("Failed to fetch cohorts from database: {}", e);
                FlagError::Internal(format!("Database query error: {}", e))
            })?;

        Ok(cohorts)
    }

    /// Parses the filters JSON into a CohortProperty structure
    // TODO: this doesn't handle the deprecated "groups" field, see
    // https://github.com/PostHog/posthog/blob/feat/dynamic-cohorts-rust/posthog/models/cohort/cohort.py#L114-L169
    // I'll handle that in a separate PR.
    pub fn parse_filters(&self) -> Result<Vec<PropertyFilter>, FlagError> {
        let filters = match &self.filters {
            Some(filters) => filters,
            None => return Ok(Vec::new()), // Return empty vec if no filters
        };

        let cohort_property: CohortProperty =
            serde_json::from_value(filters.to_owned()).map_err(|e| {
                tracing::error!("Failed to parse filters for cohort {}: {}", self.id, e);
                FlagError::CohortFiltersParsingError
            })?;

        let mut props = cohort_property.properties.to_inner();
        props.retain(|f| !(f.key == "id" && f.prop_type == "cohort"));
        Ok(props)
    }

    /// Extracts dependent CohortIds from the cohort's filters
    pub fn extract_dependencies(&self) -> Result<HashSet<CohortId>, FlagError> {
        let filters = match &self.filters {
            Some(filters) => filters,
            None => return Ok(HashSet::new()), // Return empty set if no filters
        };

        let cohort_property: CohortProperty =
            serde_json::from_value(filters.clone()).map_err(|e| {
                tracing::error!("Failed to parse filters for cohort {}: {}", self.id, e);
                FlagError::CohortFiltersParsingError
            })?;

        let mut dependencies = HashSet::new();
        Self::traverse_filters(&cohort_property.properties, &mut dependencies)?;
        Ok(dependencies)
    }

    /// Recursively traverses the filter tree to find cohort dependencies
    ///
    /// Example filter tree structure:
    /// ```json
    /// {
    ///   "properties": {
    ///     "type": "OR",
    ///     "values": [
    ///       {
    ///         "type": "OR",
    ///         "values": [
    ///           {
    ///             "key": "id",
    ///             "value": 123,
    ///             "type": "cohort",
    ///             "operator": "exact"
    ///           },
    ///           {
    ///             "key": "email",
    ///             "value": "@posthog.com",
    ///             "type": "person",
    ///             "operator": "icontains"
    ///           }
    ///         ]
    ///       }
    ///     ]
    ///   }
    /// }
    /// ```
    fn traverse_filters(
        inner: &InnerCohortProperty,
        dependencies: &mut HashSet<CohortId>,
    ) -> Result<(), FlagError> {
        for cohort_values in &inner.values {
            for filter in &cohort_values.values {
                if filter.is_cohort() {
                    // Assuming the value is a single integer CohortId
                    if let Some(cohort_id) = filter.value.as_i64() {
                        dependencies.insert(cohort_id as CohortId);
                    } else {
                        return Err(FlagError::CohortFiltersParsingError);
                    }
                }
                // NB: we don't support nested cohort properties, so we don't need to traverse further
            }
        }
        Ok(())
    }
}

impl InnerCohortProperty {
    /// Flattens the nested cohort property structure into a list of property filters.
    ///
    /// The cohort property structure in Postgres looks like:
    /// ```json
    /// {
    ///   "type": "OR",
    ///   "values": [
    ///     {
    ///       "type": "OR",
    ///       "values": [
    ///         {
    ///           "key": "email",
    ///           "value": "@posthog.com",
    ///           "type": "person",
    ///           "operator": "icontains"
    ///         },
    ///         {
    ///           "key": "age",
    ///           "value": 25,
    ///           "type": "person",
    ///           "operator": "gt"
    ///         }
    ///       ]
    ///     }
    ///   ]
    /// }
    /// ```
    pub fn to_inner(self) -> Vec<PropertyFilter> {
        self.values
            .into_iter()
            .flat_map(|value| value.values)
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{
        cohort::cohort_models::{CohortPropertyType, CohortValues},
        utils::test_utils::{
            insert_cohort_for_team_in_pg, insert_new_team_in_pg, setup_pg_reader_client,
            setup_pg_writer_client,
        },
    };
    use serde_json::json;

    #[tokio::test]
    async fn test_list_from_pg() {
        let reader = setup_pg_reader_client(None).await;
        let writer = setup_pg_writer_client(None).await;

        let team = insert_new_team_in_pg(reader.clone(), None)
            .await
            .expect("Failed to insert team");

        // Insert multiple cohorts for the team
        insert_cohort_for_team_in_pg(
            writer.clone(),
            team.id,
            Some("Cohort 1".to_string()),
            json!({"properties": {"type": "AND", "values": [{"type": "property", "values": [{"key": "age", "type": "person", "value": [30], "negation": false, "operator": "gt"}]}]}}),
            false,
        )
        .await
        .expect("Failed to insert cohort1");

        insert_cohort_for_team_in_pg(
            writer.clone(),
            team.id,
            Some("Cohort 2".to_string()),
            json!({"properties": {"type": "OR", "values": [{"type": "property", "values": [{"key": "country", "type": "person", "value": ["USA"], "negation": false, "operator": "exact"}]}]}}),
            false,
        )
        .await
        .expect("Failed to insert cohort2");

        let cohorts = Cohort::list_from_pg(reader, team.project_id)
            .await
            .expect("Failed to list cohorts");

        assert_eq!(cohorts.len(), 2);
        let names: HashSet<String> = cohorts.into_iter().filter_map(|c| c.name).collect();
        assert!(names.contains("Cohort 1"));
        assert!(names.contains("Cohort 2"));
    }

    #[test]
    fn test_cohort_parse_filters() {
        let cohort = Cohort {
            id: 1,
            name: Some("Test Cohort".to_string()),
            description: None,
            team_id: 1,
            deleted: false,
            filters: Some(
                json!({"properties": {"type": "OR", "values": [{"type": "OR", "values": [{"key": "$initial_browser_version", "type": "person", "value": ["125"], "negation": false, "operator": "exact"}]}]}}),
            ),
            query: None,
            version: None,
            pending_version: None,
            count: None,
            is_calculating: false,
            is_static: false,
            errors_calculating: 0,
            groups: json!({}),
            created_by_id: None,
        };

        let result = cohort.parse_filters().unwrap();
        assert_eq!(result.len(), 1);
        assert_eq!(result[0].key, "$initial_browser_version");
        assert_eq!(result[0].value, json!(["125"]));
        assert_eq!(result[0].prop_type, "person");
    }

    #[test]
    fn test_cohort_property_to_inner() {
        let cohort_property = InnerCohortProperty {
            prop_type: CohortPropertyType::AND,
            values: vec![CohortValues {
                prop_type: "property".to_string(),
                values: vec![
                    PropertyFilter {
                        key: "email".to_string(),
                        value: json!("test@example.com"),
                        operator: None,
                        prop_type: "person".to_string(),
                        group_type_index: None,
                        negation: None,
                    },
                    PropertyFilter {
                        key: "age".to_string(),
                        value: json!(25),
                        operator: None,
                        prop_type: "person".to_string(),
                        group_type_index: None,
                        negation: None,
                    },
                ],
            }],
        };

        let result = cohort_property.to_inner();
        assert_eq!(result.len(), 2);
        assert_eq!(result[0].key, "email");
        assert_eq!(result[0].value, json!("test@example.com"));
        assert_eq!(result[1].key, "age");
        assert_eq!(result[1].value, json!(25));
    }

    #[tokio::test]
    async fn test_extract_dependencies() {
        let reader = setup_pg_reader_client(None).await;
        let writer = setup_pg_writer_client(None).await;

        let team = insert_new_team_in_pg(reader.clone(), None)
            .await
            .expect("Failed to insert team");

        // Insert a single cohort that is dependent on another cohort
        let dependent_cohort = insert_cohort_for_team_in_pg(
            writer.clone(),
            team.id,
            Some("Dependent Cohort".to_string()),
            json!({"properties": {"type": "OR", "values": [{"type": "OR", "values": [{"key": "$browser", "type": "person", "value": ["Safari"], "negation": false, "operator": "exact"}]}]}}),
            false,
        )
        .await
        .expect("Failed to insert dependent_cohort");

        // Insert main cohort with a single dependency
        let main_cohort = insert_cohort_for_team_in_pg(
                writer.clone(),
                team.id,
                Some("Main Cohort".to_string()),
                json!({"properties": {"type": "OR", "values": [{"type": "OR", "values": [{"key": "id", "type": "cohort", "value": dependent_cohort.id, "negation": false}]}]}}),
                false,
            )
            .await
            .expect("Failed to insert main_cohort");

        let cohorts = Cohort::list_from_pg(reader.clone(), team.project_id)
            .await
            .expect("Failed to fetch cohorts");

        let fetched_main_cohort = cohorts
            .into_iter()
            .find(|c| c.id == main_cohort.id)
            .expect("Failed to find main cohort");

        let dependencies = fetched_main_cohort.extract_dependencies().unwrap();
        let expected_dependencies: HashSet<CohortId> =
            [dependent_cohort.id].iter().cloned().collect();

        assert_eq!(dependencies, expected_dependencies);
    }
}
