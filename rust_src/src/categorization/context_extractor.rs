pub fn extract_contexts(abstract_text: &str, entities: &[String]) -> Vec<(String, String)> {
    let sentences: Vec<&str> = abstract_text.split(". ").collect();
    let mut contexts = Vec::new();
    
    for entity in entities {
        for sentence in &sentences {
            if sentence.to_lowercase().contains(&entity.to_lowercase()) {
                contexts.push((entity.clone(), sentence.to_string()));
            }
        }
    }
    contexts
}
